"""Tests for the settle/begin rewire (issue #583, unit ``settlement-rewire``).

This is the unit that makes the previous six do anything: until it landed,
``workflow_journal.begin_step`` and ``settle_step`` had no production caller and Bolt 1A
changed no behaviour. So these tests are where several sibling guarantees stop being
theoretical:

- **BR-1/BR-5** — the ``v2`` fingerprint is computed inside ``run_agent_step``, after
  working-directory resolution and before terminal creation. The mutation that matters is
  computing it from the POSTED ``working_directory`` instead of the RESOLVED one; the
  ``caller_id``-inheritance tests below fail under exactly that mutation and pass under
  nothing else.
- **BR-3** — the terminal-ready hook fires on the terminal-REUSE path too, so every script
  step has a durable ``running`` row before it executes rather than only the ones that made
  their own terminal.
- **BR-6/BR-7** — one ``settle_step`` replaces the ``append_step`` + ``update_step`` pair, and
  its returned bool is logged as an OBSERVATION ("no prior row observed at settle") because
  the bool is asymmetric and ``False`` can fire spuriously in a two-process race.
- **SR-1..SR-6** — this unit pays Bolt 1A's redaction debt. ``error`` is redacted THEN bounded
  tail-first THEN marked; ``output_json`` is redacted STRUCTURALLY and replaced rather than
  truncated. Three tests carry that section and each is easy to write weakly:
  the straddling-credential fixture (a credential safely inside the kept region passes under
  either order and proves nothing), the marker-inclusive size assertion (catches prepending
  after bounding to the full cap), and the nested-credential-plus-still-parses pair (a
  corrupting redaction passes the redaction half alone).

No credential in this file is real: every fixture is a synthetic value shaped to match a
``secret_gate`` pattern.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.constants import WORKFLOW_JOURNAL_RESULT_MAX_BYTES as CAP
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.models.workflow_runtime import RunState, StepState
from cli_agent_orchestrator.services import (
    agent_step,
    script_runner,
    workflow_journal,
    workflow_service,
)
from cli_agent_orchestrator.services.agent_step import run_agent_step
from cli_agent_orchestrator.services.script_runner import (
    _ERROR_TRUNCATION_MARKER,
    ScriptRunRecord,
    _sanitise_error,
    _sanitise_output_json,
    make_step_terminal_recorder,
    record_step_completion,
)
from cli_agent_orchestrator.services.secret_gate import redact_secrets
from cli_agent_orchestrator.services.step_fingerprint import (
    CREATION_ONLY,
    StepCallFields,
    compute,
    scheme_of,
)
from cli_agent_orchestrator.services.workflow_service import RunRecord, StepRunState

_AGENT_STEP = "cli_agent_orchestrator.services.agent_step"

# Synthetic credential fixture — shaped to match ``secret_gate``'s ``aws_access_key``, never
# real. It is the pattern of choice for the boundary tests because its match is FIXED-LENGTH
# (20 chars): ``bearer_token``'s trailing ``\S{16,}`` is greedy and would swallow the filler
# either side of it, so a straddle could not be positioned deliberately. ``bearer_token`` is
# exercised in the ``output_json`` tests instead, where that greed is the point.
_FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"  # 20 chars


# ---------------------------------------------------------------------------
# Fixtures + harness
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Temp SQLite journal + an isolated process-local registry around each test."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()
    yield db_path
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()


def _script_record(run_id: str) -> ScriptRunRecord:
    """Register a live ``ScriptRunRecord`` — the guard both callbacks require."""
    record = ScriptRunRecord(
        run_id=run_id,
        workflow_name="wf",
        state=RunState.RUNNING,
        cancelled=False,
        current_step_id=None,
        step_states={},
        process=None,
        generation="1",
        started_at="2026-08-16T00:00:00Z",
        finished_at=None,
    )
    workflow_service.run_registry[run_id] = record
    return record


def _env(run_id: str, step_id: str) -> Dict[str, str]:
    """The run/step env a genuine script run-step call carries."""
    return {"CAO_WORKFLOW_RUN_ID": run_id, "CAO_WORKFLOW_STEP_ID": step_id}


def _fake_terminal(terminal_id: str = "term-new"):
    t = MagicMock()
    t.id = terminal_id
    return t


def _patch_terminal_layer(
    *,
    created_id: str = "term-new",
    output: str = "the answer",
    get_wd_return: Optional[str] = None,
    reuse_provider: str = "kiro_cli",
    on_send: Optional[Any] = None,
):
    """Patch the whole terminal layer ``run_agent_step`` drives (mirrors test_agent_step).

    ``on_send`` is invoked by the ``send_input`` stand-in, which is the instant AFTER the
    terminal-ready hook and BEFORE the step executes — the window BR-3's "durable ``running``
    row before it executes" claim is about.
    """

    def _send(terminal_id: str, prompt: str) -> bool:
        if on_send is not None:
            on_send()
        return True

    return (
        patch(
            f"{_AGENT_STEP}.terminal_service.create_terminal",
            new=AsyncMock(return_value=_fake_terminal(created_id)),
        ),
        patch(f"{_AGENT_STEP}.terminal_service.send_input", new=_send),
        patch(f"{_AGENT_STEP}.terminal_service.delete_terminal", return_value=True),
        patch(f"{_AGENT_STEP}.terminal_service.get_output", return_value=output),
        patch(f"{_AGENT_STEP}.terminal_service.exit_terminal_cli", return_value=None),
        patch(f"{_AGENT_STEP}.wait_until_status", new=AsyncMock(return_value=True)),
        patch(
            f"{_AGENT_STEP}.status_monitor.get_status",
            return_value=TerminalStatus.COMPLETED,
        ),
        patch(
            f"{_AGENT_STEP}.terminal_service.get_working_directory",
            return_value=get_wd_return,
        ),
        patch(
            f"{_AGENT_STEP}.terminal_service.get_terminal_metadata",
            return_value={"id": "reuse-1", "provider": reuse_provider, "engine": "v2"},
        ),
    )


def _drive(hook: Any = None, *, layer: Optional[Dict[str, Any]] = None, **step_kwargs: Any):
    """Run one ``run_agent_step`` against the mocked terminal layer."""
    patches = _patch_terminal_layer(**(layer or {}))
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
    ):
        return asyncio.run(run_agent_step(on_step_terminal_ready=hook, **step_kwargs))


def _capture(**step_kwargs: Any) -> List[Tuple[str, str]]:
    """Drive a step with a capturing hook; return the ``(terminal_id, fingerprint)`` pairs."""
    seen: List[Tuple[str, str]] = []
    layer = step_kwargs.pop("layer", None)
    _drive(lambda tid, fp: seen.append((tid, fp)), layer=layer, **step_kwargs)
    return seen


def _base_fields(**overrides: Any) -> StepCallFields:
    fields: Dict[str, Any] = {"provider": "kiro_cli", "agent": "dev", "prompt": "go"}
    fields.update(overrides)
    return StepCallFields(**fields)


# ---------------------------------------------------------------------------
# BR-1 — the fingerprint is computed from the EFFECTIVE working directory
# ---------------------------------------------------------------------------
def test_fingerprint_differs_when_the_explicit_working_directory_differs():
    """The baseline: two calls differing only in ``working_directory`` are not one identity."""
    a = _capture(provider="kiro_cli", agent="dev", prompt="go", working_directory="/wd/a")
    b = _capture(provider="kiro_cli", agent="dev", prompt="go", working_directory="/wd/b")
    assert a[0][1] != b[0][1]
    assert a[0][1] == compute(_base_fields(effective_working_directory="/wd/a"))
    assert b[0][1] == compute(_base_fields(effective_working_directory="/wd/b"))


def test_fingerprint_differs_when_only_the_INHERITED_cwd_differs():
    """BR-1/BR-5, and the mutation this whole group exists for.

    Both calls post ``working_directory=None``; only the CALLER's pane CWD differs, and
    ``run_agent_step`` resolves it before creating the terminal. A fingerprint computed from
    the posted value would be IDENTICAL for both, so one run would replay the other's result
    even though the two agents worked in different real directories. This test fails under
    exactly that mutation.
    """
    a = _capture(
        provider="kiro_cli",
        agent="dev",
        prompt="go",
        working_directory=None,
        caller_id="sup-1",
        layer={"get_wd_return": "/inherited/a"},
    )
    b = _capture(
        provider="kiro_cli",
        agent="dev",
        prompt="go",
        working_directory=None,
        caller_id="sup-1",
        layer={"get_wd_return": "/inherited/b"},
    )
    assert a[0][1] != b[0][1]


def test_the_inherited_cwd_is_the_hashed_one_not_the_posted_None():
    """The positive form of the rule: the digest equals the RESOLVED directory's digest.

    Asserting both halves — that it matches the resolved value AND does not match the posted
    ``None`` — is what makes the test unable to pass under the wrong computation.
    """
    seen = _capture(
        provider="kiro_cli",
        agent="dev",
        prompt="go",
        working_directory=None,
        caller_id="sup-1",
        layer={"get_wd_return": "/inherited/real"},
    )
    assert seen[0][1] == compute(_base_fields(effective_working_directory="/inherited/real"))
    assert seen[0][1] != compute(_base_fields(effective_working_directory=None))


# ---------------------------------------------------------------------------
# BR-3 — a durable ``running`` row exists before the step executes, on BOTH paths
# ---------------------------------------------------------------------------
def _row_state_at_send(run_id: str, step_id: str) -> Dict[str, Any]:
    """Capture the durable row as it stands at the ``send_input`` instant."""
    captured: Dict[str, Any] = {}

    def _peek() -> None:
        row = workflow_journal.get_step(run_id, step_id)
        captured["row"] = row
        captured["state"] = None if row is None else row.state
        captured["fingerprint"] = None if row is None else row.call_fingerprint

    return {"peek": _peek, "captured": captured}


def test_create_path_writes_a_running_row_before_the_step_executes():
    """The create path: ``begin_step`` has fired, with a fingerprint, before the prompt is sent."""
    record = _script_record("run-create")
    hook = make_step_terminal_recorder(_env("run-create", "s1"))
    assert hook is not None
    probe = _row_state_at_send("run-create", "s1")

    _drive(
        hook,
        provider="kiro_cli",
        agent="dev",
        prompt="go",
        layer={"on_send": probe["peek"]},
    )

    assert probe["captured"]["state"] == "running"
    assert scheme_of(probe["captured"]["fingerprint"]) == "v2"
    assert record.step_states["s1"].call_fingerprint == probe["captured"]["fingerprint"]


def test_reuse_path_writes_a_running_row_before_the_step_executes():
    """BR-3's whole point: a terminal-REUSE call gets a durable ``running`` row too.

    Before this unit the hook fired only inside ``if created_here:``, so a reuse call wrote no
    row at all and FR-4's guard covered only steps that made their own terminal. Deleting the
    reuse-path invocation fails this test and nothing else.
    """
    record = _script_record("run-reuse")
    hook = make_step_terminal_recorder(_env("run-reuse", "s1"))
    assert hook is not None
    probe = _row_state_at_send("run-reuse", "s1")

    result = _drive(
        hook,
        provider="kiro_cli",
        agent="dev",
        prompt="go",
        reuse_terminal_id="reuse-1",
        layer={"on_send": probe["peek"]},
    )

    assert result.terminal_id == "reuse-1"
    assert probe["captured"]["state"] == "running"
    assert scheme_of(probe["captured"]["fingerprint"]) == "v2"
    assert record.step_states["s1"].terminal_id == "reuse-1"


def test_the_create_path_notifies_BEFORE_the_readiness_wait():
    """BR-31's window, which this unit had to move code through without widening.

    The readiness wait can run for ``ready_timeout`` seconds (120 by default), and the
    dangerous edge BR-31 closed is a subprocess dying while a run-step call is mid-flight.
    Notifying after that wait would satisfy "before the step executes" and still reopen the
    gap — so the ORDER is asserted, not just the fact.
    """
    order: List[str] = []

    async def _watched_wait(*a: Any, **k: Any) -> bool:
        order.append("readiness-wait")
        return True

    patches = _patch_terminal_layer()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[6],
        patches[7],
        patches[8],
        patch(f"{_AGENT_STEP}.wait_until_status", new=_watched_wait),
    ):
        asyncio.run(
            run_agent_step(
                provider="kiro_cli",
                agent="dev",
                prompt="go",
                on_step_terminal_ready=lambda tid, fp: order.append("hook"),
            )
        )

    assert order == ["hook", "readiness-wait"]


def test_both_paths_publish_a_fingerprint_onto_the_step_state():
    """BR-2: the in-memory carrier is populated on both paths, by the hook closure."""
    created = _script_record("run-c2")
    hook_created = make_step_terminal_recorder(_env("run-c2", "s1"))
    _drive(hook_created, provider="kiro_cli", agent="dev", prompt="go")

    reused = _script_record("run-r2")
    hook_reused = make_step_terminal_recorder(_env("run-r2", "s1"))
    _drive(hook_reused, provider="kiro_cli", agent="dev", prompt="go", reuse_terminal_id="reuse-1")

    for record in (created, reused):
        published = record.step_states["s1"].call_fingerprint
        assert published is not None
        assert scheme_of(published) == "v2"
    # Same provider/agent/prompt, different paths -> different identities (``reused_terminal``
    # is itself a hashed component, so the two populations cannot collide).
    assert created.step_states["s1"].call_fingerprint != reused.step_states["s1"].call_fingerprint


# ---------------------------------------------------------------------------
# BR-4 — the hook is renamed, because firing it on reuse made the old name false
# ---------------------------------------------------------------------------
def _src_files() -> List[Path]:
    root = Path(inspect.getsourcefile(script_runner) or "").parents[1]
    return sorted(root.rglob("*.py"))


def test_the_old_callback_name_appears_nowhere_in_src():
    """``on_terminal_created`` is GONE, not aliased beside the new name.

    Leaving it would be the third shipped claim in this Bolt that its own code contradicts.
    """
    offenders = [
        str(path)
        for path in _src_files()
        if "on_terminal_created" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def _hook_doc_block() -> str:
    """The ``on_step_terminal_ready`` entry of ``run_agent_step``'s Args section.

    ``inspect.getdoc`` dedents, so an Args KEY sits at four spaces and its continuation lines
    at eight. The block therefore ends at the first non-blank line that is not eight-space
    indented — which is what keeps the neighbouring entries (several of which legitimately say
    "freshly created terminal") out of the assertion.
    """
    doc = inspect.getdoc(run_agent_step) or ""
    lines = doc.splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.strip().startswith("on_step_terminal_ready:")
    )
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("        "):
            break
        block.append(line)
    assert len(block) > 3, "the hook's Args entry did not extract — the docstring shape changed"
    return "\n".join(block)


def test_neither_docstring_still_says_created():
    """BR-4: the hook fires on the reuse path, so "created" is no longer true of it.

    Scoped to the two docstrings that describe the hook — ``run_agent_step``'s Args entry and
    the factory that builds it — rather than to whole files, because both modules legitimately
    describe terminal creation elsewhere.
    """
    hook_doc = _hook_doc_block()
    assert "created" not in hook_doc, hook_doc
    factory_doc = inspect.getdoc(make_step_terminal_recorder) or ""
    assert "created" not in factory_doc, factory_doc
    # And the replacement really does describe both paths, so the rename is not cosmetic.
    assert "reuse" in hook_doc


# ---------------------------------------------------------------------------
# BR-5 — a reuse call's fingerprint carries the sentinels, and that is CORRECT
# ---------------------------------------------------------------------------
def test_a_reuse_fingerprint_ignores_every_creation_only_field():
    """BR-1a/BR-5: two reuse calls differing ONLY in creation-only inputs are one identity.

    ``run_agent_step`` discards ``model``, ``allowed_tools``, the effective directory and
    ``use_worktree`` on the reuse path, so hashing their values would manufacture a false
    ``DIVERGED`` and demand a human decision that has no meaning. Stated as a rule so nobody
    later "fixes" it.
    """
    a = _capture(
        provider="kiro_cli",
        agent="dev",
        prompt="go",
        reuse_terminal_id="reuse-1",
        model="sonnet",
        allowed_tools=["Read"],
        working_directory="/wd/a",
        use_worktree=True,
    )
    b = _capture(
        provider="kiro_cli",
        agent="dev",
        prompt="go",
        reuse_terminal_id="reuse-1",
        model="haiku",
        allowed_tools=["Read", "Write"],
        working_directory="/wd/b",
        use_worktree=False,
    )
    assert a[0][1] == b[0][1]


def test_a_reuse_fingerprint_is_the_ten_component_sentinel_form():
    """The tuple is never shortened: ten components on both paths.

    Arity is asserted by EQUALITY with ``compute``, which always joins exactly ten components
    — so a reuse digest matching it cannot have been built from nine.
    """
    seen = _capture(
        provider="kiro_cli",
        agent="dev",
        prompt="go",
        reuse_terminal_id="reuse-1",
        model="sonnet",
        working_directory="/wd/ignored",
    )
    expected = compute(
        _base_fields(
            model="anything-at-all",
            effective_working_directory="/somewhere/else",
            use_worktree=True,
            reused_terminal=True,
        )
    )
    assert seen[0][1] == expected
    assert scheme_of(seen[0][1]) == "v2"
    # The sentinel is a positional substitution, not a shortening: a create call whose
    # creation-only fields literally hold the sentinel STRING is still a different identity.
    assert seen[0][1] != compute(
        _base_fields(
            model=CREATION_ONLY,
            effective_working_directory=CREATION_ONLY,
            use_worktree=False,
            reused_terminal=False,
        )
    )


# ---------------------------------------------------------------------------
# BR-6 — ONE settle_step replaces the append_step + update_step pair
# ---------------------------------------------------------------------------
def _journal_calls_in(module: Any) -> List[str]:
    """Every ``workflow_journal.<fn>(...)`` called from ``module``'s source, via AST.

    Parsed rather than string-matched so a docstring discussing ``append_step`` in prose
    cannot produce a false failure — which matters here, because both docstrings do.
    """
    tree = ast.parse(Path(inspect.getsourcefile(module) or "").read_text(encoding="utf-8"))
    names: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        if isinstance(target, ast.Name) and target.id == "workflow_journal":
            names.append(node.func.attr)
    return names


def test_the_script_tier_no_longer_calls_append_step_or_update_step():
    """BR-6: the two-write pair is gone from ``script_runner``, replaced by one settle."""
    called = _journal_calls_in(script_runner)
    assert "append_step" not in called
    assert "update_step" not in called
    assert "settle_step" in called
    assert "begin_step" in called


def test_append_step_and_update_step_keep_their_signatures_and_yaml_callers():
    """Unit 6 BR-10: this unit is additive at the journal boundary, not a rewrite.

    The YAML tier still drives ``update_step``; changing the script tier must not have touched
    either helper's shape.
    """
    assert list(inspect.signature(workflow_journal.append_step).parameters) == [
        "run_id",
        "step_id",
        "state",
        "updated_at",
        "call_fingerprint",
    ]
    # MERGE NOTE (2026-08-17, #583 x #504): ``error_kind`` is #504's addition to
    # ``update_step``, NOT this change's — ``git diff 135e7ff..949eab1`` shows #583 never
    # touched this signature. The guard keeps its full force: it still proves #583 left
    # ``append_step``/``update_step`` alone, so the expected list is brought up to current
    # reality rather than the assertion being relaxed.
    assert list(inspect.signature(workflow_journal.update_step).parameters) == [
        "run_id",
        "step_id",
        "state",
        "attempts",
        "updated_at",
        "output_json",
        "error",
        "error_kind",
    ]
    assert "update_step" in _journal_calls_in(workflow_service)


def test_a_settle_makes_exactly_one_settle_step_call(monkeypatch: pytest.MonkeyPatch):
    """One write, and no ``attempts`` argument — the parameter does not exist (unit 6 BR-6)."""
    record = _script_record("run-once")
    record.step_states["s1"] = StepRunState(step_id="s1", state=StepState.RUNNING)
    calls: List[Dict[str, Any]] = []
    real = workflow_journal.settle_step

    def _counting(**kwargs: Any) -> bool:
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(workflow_journal, "settle_step", _counting)
    settle = record_step_completion(_env("run-once", "s1"))
    assert settle is not None
    settle("term-1", None, "done")

    assert len(calls) == 1
    assert "attempts" not in calls[0]
    assert workflow_journal.get_step("run-once", "s1").attempts == 1


# ---------------------------------------------------------------------------
# BR-7 / SR-8 — the returned bool is logged as an OBSERVATION, never a conclusion
# ---------------------------------------------------------------------------
def test_a_settle_with_no_prior_row_logs_the_exact_observation(caplog: pytest.LogCaptureFixture):
    """The wording is BINDING (unit 6 TD-2a), not advisory.

    ``settle_step``'s bool is asymmetric: its pre-upsert ``SELECT`` shares no transaction with
    its upsert, so ``False`` can be reported while the row did exist. A conclusion-shaped
    message ("the callback never fired") would send a human hunting a bug that did not happen.
    """
    _script_record("run-nobegin")
    settle = record_step_completion(_env("run-nobegin", "s1"))
    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.script_runner"):
        settle("term-1", None, "done")

    hits = [r for r in caplog.records if "no prior row observed at settle" in r.getMessage()]
    assert len(hits) == 1
    # SR-8: two identifiers, and no third. ``%s``-style args are the assertion surface.
    assert hits[0].args == ("run-nobegin", "s1")
    message = hits[0].getMessage()
    assert "never fired" not in message
    assert "callback" not in message


def test_a_settle_after_a_begin_logs_no_such_warning(caplog: pytest.LogCaptureFixture):
    """The complement: the observation fires on absence only, so it stays diagnostic."""
    record = _script_record("run-withbegin")
    hook = make_step_terminal_recorder(_env("run-withbegin", "s1"))
    _drive(hook, provider="kiro_cli", agent="dev", prompt="go")
    settle = record_step_completion(_env("run-withbegin", "s1"))
    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.script_runner"):
        settle(record.step_states["s1"].terminal_id, None, "the answer")

    assert [r for r in caplog.records if "no prior row observed" in r.getMessage()] == []


# ---------------------------------------------------------------------------
# SR-1 — redact THEN bound: the straddling-credential fixture
# ---------------------------------------------------------------------------
def _straddling_error(secret: str, *, head: int = 100) -> Tuple[str, int]:
    """Build an ``error`` whose truncation boundary falls INSIDE ``secret``.

    Returns the text and the byte offset the boundary lands on, so the test can prove the
    construction really straddles rather than merely assuming it — a secret that ends up
    safely inside the kept region passes under EITHER order and proves nothing.
    """
    # The marker's reserved length for any input in this size range (its count is 5 digits).
    reserve = len(_ERROR_TRUNCATION_MARKER.format(dropped=CAP + 1000))
    keep = CAP - reserve
    # Place the secret so it starts 10 bytes before the kept region begins.
    tail = keep - 10
    text = ("H" * head) + secret + ("T" * tail)
    boundary = len(text.encode("utf-8")) - keep
    return text, boundary


def test_a_credential_straddling_the_byte_boundary_is_fully_redacted():
    """SR-1: the ONE case that distinguishes redact-then-bound from bound-then-redact.

    Under the wrong order the boundary cuts the credential in half, the pattern no longer
    matches the surviving fragment, and that fragment is persisted verbatim. A partial
    credential is not safe for being partial.
    """
    text, boundary = _straddling_error(_FAKE_AWS_KEY)
    # Self-check: the construction must actually straddle, or the test proves nothing.
    assert 100 < boundary < 100 + len(_FAKE_AWS_KEY), boundary

    stored = _sanitise_error(text)
    assert stored is not None
    # The kept-side half of the credential must not survive. This is the assertion that
    # fails when the order is swapped.
    assert _FAKE_AWS_KEY[10:] not in stored
    assert "AKIA" not in stored
    # Redaction happened before the cut, so the marker's own tail is what got kept.
    assert "ccess_key]" in stored
    assert len(stored.encode("utf-8")) <= CAP


@pytest.mark.parametrize("offset", [-8, -4, 0, 4, 8])
def test_no_fragment_of_a_boundary_straddling_credential_survives(offset: int):
    """The same property swept across the boundary, so it cannot pass by lucky alignment."""
    text, _boundary = _straddling_error(_FAKE_AWS_KEY, head=100 + offset)
    stored = _sanitise_error(text)
    assert stored is not None
    for start in range(0, len(_FAKE_AWS_KEY) - 8):
        assert _FAKE_AWS_KEY[start : start + 8] not in stored


# ---------------------------------------------------------------------------
# SR-2 — truncation keeps the TAIL, deliberately diverging from the envelope
# ---------------------------------------------------------------------------
def test_an_over_long_error_keeps_its_final_bytes():
    """A traceback's meaning is back-loaded: the exception line is the part worth keeping."""
    error = "Traceback (most recent call last):\n" + ("frame\n" * 40_000) + "ValueError: boom"
    stored = _sanitise_error(error)
    assert stored is not None
    assert stored.endswith("ValueError: boom")
    # The head really was dropped, so this is tail-retention and not a no-op.
    assert "Traceback (most recent call last):" not in stored
    assert len(stored.encode("utf-8")) <= CAP


def test_error_keeps_the_suffix_where_the_envelope_keeps_the_prefix():
    """The asymmetry is deliberate (SR-2/TD-6) and will otherwise read as drift.

    Same input, two columns, opposite ends kept — because a message's meaning is front-loaded
    and a traceback's is back-loaded.
    """
    from cli_agent_orchestrator.services.step_result import build_envelope

    text = "HEAD-MARKER" + ("m" * (CAP + 5000)) + "TAIL-MARKER"
    envelope = build_envelope(text, "failed", None)
    stored = _sanitise_error(text)
    assert stored is not None

    assert envelope.last_message.startswith("HEAD-MARKER")
    assert not envelope.last_message.endswith("TAIL-MARKER")
    assert stored.endswith("TAIL-MARKER")
    assert "HEAD-MARKER" not in stored


# ---------------------------------------------------------------------------
# SR-3 — the marker is sized INTO the bound
# ---------------------------------------------------------------------------
def test_a_truncated_error_carries_the_marker():
    """``error`` has no ``truncated`` flag column, so a silent truncation would read as a
    MALFORMED traceback rather than a truncated one."""
    stored = _sanitise_error("x" * (CAP + 5000))
    assert stored is not None
    assert stored.startswith("[... error truncated:")
    assert "leading bytes dropped" in stored


@pytest.mark.parametrize("excess", [1, 2, 70, 71, 5000, CAP])
def test_the_stored_error_never_exceeds_the_cap_INCLUDING_the_marker(excess: int):
    """The off-by-one catcher (SR-3).

    Bounding to the full cap and THEN prepending the marker pushes the column past the very
    cap the rule enforces. Sizes just above the bound and just around the marker's own length
    are where that shows.
    """
    stored = _sanitise_error("y" * (CAP + excess))
    assert stored is not None
    assert len(stored.encode("utf-8")) <= CAP


def test_an_error_at_or_under_the_cap_is_untouched_and_unmarked():
    """Inclusive boundary, matching ``build_envelope``: exactly the bound is NOT truncated."""
    exact = "z" * CAP
    assert _sanitise_error(exact) == exact
    assert _sanitise_error("short failure") == "short failure"
    assert _ERROR_TRUNCATION_MARKER[:20] not in (_sanitise_error(exact) or "")
    assert _sanitise_error(None) is None


# ---------------------------------------------------------------------------
# SR-4 — output_json is transformed STRUCTURALLY, never as text
# ---------------------------------------------------------------------------
def test_a_credential_nested_three_levels_deep_is_redacted_and_the_result_still_parses():
    """Both halves in ONE test, or a redaction that corrupts the document passes the
    redaction assertion (TD-6's test-shape note).

    The deep credential is deliberately shaped so that a TEXT-level redaction of the
    serialised document would run past its closing quote and destroy validity — the first
    draft of this test used a credential whose replacement was structurally harmless, and a
    text-level mutant passed it. So both halves are now mechanism-sensitive, not just the
    redaction half.
    """
    deep_secret = "token=" + "abcdefghijklmnopqrstuvwx"
    document = {
        "a": {"b": {"c": deep_secret}},
        "also": f"connect failed using {_FAKE_AWS_KEY}",
        "keep": [1, 2, 3],
    }
    stored = _sanitise_output_json(json.dumps(document))
    assert stored is not None

    assert deep_secret not in stored
    assert _FAKE_AWS_KEY not in stored
    assert "[REDACTED:aws_access_key]" in stored
    # The other half: it is still a document, with its shape intact all the way down.
    parsed = json.loads(stored)
    assert parsed["keep"] == [1, 2, 3]
    assert parsed["a"]["b"]["c"] == "[REDACTED:bearer_token]"
    assert "[REDACTED:aws_access_key]" in parsed["also"]


def test_a_text_level_redaction_would_corrupt_the_document_and_this_one_does_not():
    """SR-4's reason, asserted rather than asserted-about.

    ``bearer_token``'s ``\\S{16,}`` can match ACROSS a closing quote and into the next key, so
    a ``redact_secrets`` run over the SERIALISED text destroys validity — and the failure would
    surface at READ time, far from the write. The control assertion proves the hazard is real
    for this exact input; the subject assertion proves the structural walk avoids it.
    """
    secret_value = "abcdefghijklmnopqrstuvwx"
    raw = json.dumps({"cmd": f"token={secret_value}", "next": "keepme"}, separators=(",", ":"))

    # Control: over the SERIALISED text, ``\S{16,}`` runs straight through the closing quote,
    # the comma, the next key and its value — so the replacement eats the rest of the document.
    corrupted, fired = redact_secrets(raw)
    assert fired == ["bearer_token"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(corrupted)

    # Subject: redaction that only ever sees leaf strings cannot break structure it never saw.
    stored = _sanitise_output_json(raw)
    assert stored is not None
    parsed = json.loads(stored)
    assert secret_value not in stored
    assert parsed["cmd"] == "[REDACTED:bearer_token]"
    assert parsed["next"] == "keepme"


def test_an_unparseable_output_becomes_the_placeholder_rather_than_a_guess():
    """It was never a valid document, so its shape is unknown and text-level surgery on it is
    exactly what SR-4 refuses."""
    stored = _sanitise_output_json("{not json at all")
    assert stored is not None
    parsed = json.loads(stored)
    assert parsed["cao_output_dropped"] == "unparseable"
    assert _sanitise_output_json(None) is None


# ---------------------------------------------------------------------------
# SR-5 — an over-bound output_json becomes a VALID placeholder, never a truncation
# ---------------------------------------------------------------------------
def test_an_over_large_output_becomes_a_valid_placeholder_that_records_the_drop():
    """Truncating JSON at a byte offset invalidates it, and BOTH readers parse this column."""
    raw = json.dumps({"payload": "q" * (CAP * 2)})
    stored = _sanitise_output_json(raw)
    assert stored is not None

    parsed = json.loads(stored)  # must parse — the whole point
    assert parsed["cao_output_dropped"] == "oversize"
    assert parsed["original_bytes"] > CAP
    # NOT a truncation: a prefix of the original would be an invalid document.
    assert not raw.startswith(stored)
    assert len(stored.encode("utf-8")) <= CAP


def test_an_output_at_the_cap_is_kept_intact():
    """The replacement fires on EXCESS only; a document that fits is persisted as itself."""
    payload = "r" * (CAP - 100)
    raw = json.dumps({"payload": payload}, separators=(",", ":"))
    assert len(raw.encode("utf-8")) <= CAP
    stored = _sanitise_output_json(raw)
    assert stored is not None
    assert json.loads(stored)["payload"] == payload


# ---------------------------------------------------------------------------
# SR-6 — lossy but TOTAL: neither transformation ever fails a step
# ---------------------------------------------------------------------------
def test_a_ten_megabyte_error_still_settles():
    """Turning a persistence limit into a run failure would let a verbose agent fail runs by
    talking too much, and a settle that rejected would strand a step that already succeeded."""
    record = _script_record("run-huge-e")
    record.step_states["s1"] = StepRunState(step_id="s1", state=StepState.RUNNING)
    settle = record_step_completion(_env("run-huge-e", "s1"))
    settle("term-1", "E" * (10 * 1024 * 1024), None)  # must not raise

    row = workflow_journal.get_step("run-huge-e", "s1")
    assert row is not None and row.state == "failed"
    assert row.error is not None and len(row.error.encode("utf-8")) <= CAP


def test_a_ten_megabyte_structured_output_still_settles():
    """The same posture for the structured column, which is replaced rather than truncated."""
    from cli_agent_orchestrator.services.step_output_store import record_step_output

    record = _script_record("run-huge-o")
    record.step_states["s1"] = StepRunState(step_id="s1", state=StepState.RUNNING)
    record_step_output("run-huge-o", "s1", {"blob": "O" * (10 * 1024 * 1024)})
    settle = record_step_completion(_env("run-huge-o", "s1"))
    settle("term-1", None, "done")  # must not raise

    row = workflow_journal.get_step("run-huge-o", "s1")
    assert row is not None and row.state == "completed"
    assert row.output_json is not None
    assert json.loads(row.output_json)["cao_output_dropped"] == "oversize"


# ---------------------------------------------------------------------------
# SR-7 — the digest is never logged, echoed, or put in a message
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("module", [agent_step, script_runner, workflow_service])
def test_no_logging_call_references_a_fingerprint_variable(module: Any):
    """SR-1's benefit in ``step_fingerprint`` is destroyed by one helpful log line.

    Inspects NAMES and ATTRIBUTES only — a docstring or format string that discusses the
    fingerprint in prose is not an echo of its value.
    """
    tree = ast.parse(Path(inspect.getsourcefile(module) or "").read_text(encoding="utf-8"))
    offenders: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        if not (isinstance(target, ast.Name) and target.id in {"logger", "logging"}):
            continue
        for argument in list(node.args) + [kw.value for kw in node.keywords]:
            for inner in ast.walk(argument):
                if isinstance(inner, ast.Name) and "fingerprint" in inner.id:
                    offenders.append(f"{module.__name__}:{node.lineno} {inner.id}")
                if isinstance(inner, ast.Attribute) and "fingerprint" in inner.attr:
                    offenders.append(f"{module.__name__}:{node.lineno} .{inner.attr}")
    assert offenders == []


# ---------------------------------------------------------------------------
# BR-9 / SR-9 — the YAML/handoff no-op guard is unchanged
# ---------------------------------------------------------------------------
def test_both_factories_stay_none_without_a_live_script_record():
    """No run/step env, a partial env, or a run absent from the registry -> no callback."""
    assert make_step_terminal_recorder(None) is None
    assert record_step_completion(None) is None
    assert make_step_terminal_recorder({"CAO_WORKFLOW_RUN_ID": "x"}) is None
    assert record_step_completion({"CAO_WORKFLOW_STEP_ID": "s1"}) is None
    assert make_step_terminal_recorder(_env("ghost", "s1")) is None
    assert record_step_completion(_env("ghost", "s1")) is None


def test_a_yaml_tier_record_reaches_neither_callback():
    """A live but non-``ScriptRunRecord`` registry entry is the YAML tier — wholly unaffected."""
    from cli_agent_orchestrator.models.workflow import WorkflowSpec

    workflow_service.run_registry["run-yaml"] = RunRecord(
        run_id="run-yaml",
        workflow_name="wf",
        spec=WorkflowSpec.model_validate(
            {
                "name": "wf",
                "version": "1",
                "mode": "sequential",
                "steps": [
                    {"id": "s1", "provider": "kiro_cli", "agent": "developer", "prompt": "do it"}
                ],
            }
        ),
        inputs={},
    )
    assert make_step_terminal_recorder(_env("run-yaml", "s1")) is None
    assert record_step_completion(_env("run-yaml", "s1")) is None


def test_a_handoff_call_writes_nothing_and_redacts_nothing(monkeypatch: pytest.MonkeyPatch):
    """SR-9: for a handoff caller the new code is not reached AT ALL.

    Booby-trapping the journal writers and the redactor is what makes "not reached" an
    assertion rather than a claim.
    """
    for name in ("begin_step", "settle_step", "append_step", "update_step"):

        def _boom(*a: Any, **k: Any) -> None:
            raise AssertionError(f"journal.{name} must not be reached by a handoff call")

        monkeypatch.setattr(workflow_journal, name, _boom)

    def _no_redaction(content: str):
        raise AssertionError("redact_secrets must not be reached by a handoff call")

    monkeypatch.setattr(script_runner, "redact_secrets", _no_redaction)

    # A handoff call carries no run/step env, so both factories are None.
    hook = make_step_terminal_recorder(None)
    settle = record_step_completion(None)
    assert hook is None and settle is None
    result = _drive(hook, provider="kiro_cli", agent="dev", prompt="go")
    assert result.status == TerminalStatus.COMPLETED


# ---------------------------------------------------------------------------
# BR-10 / INV-4 — a journal failure degrades resumability and never fails a step
# ---------------------------------------------------------------------------
def test_a_begin_step_failure_is_swallowed_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """The swallow at this site is NEW — there was no swallow there because there was no call.

    A failure to write the RUNNING row must not fail a step that is about to run, and the
    in-memory bookkeeping the sweep depends on must still land.
    """
    record = _script_record("run-beginboom")

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(workflow_journal, "begin_step", _boom)
    hook = make_step_terminal_recorder(_env("run-beginboom", "s1"))
    assert hook is not None
    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.script_runner"):
        hook("term-1", "v2:" + "a" * 64)  # must not raise

    assert record.step_states["s1"].terminal_id == "term-1"
    assert record.step_states["s1"].call_fingerprint == "v2:" + "a" * 64
    hits = [r for r in caplog.records if "failed to write the running row" in r.getMessage()]
    assert len(hits) == 1
    # SR-7: the failure line must not carry the digest.
    assert "a" * 64 not in hits[0].getMessage()


def test_a_settle_step_failure_is_swallowed_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """The pre-existing posture, preserved: the in-memory transition still lands."""
    record = _script_record("run-settleboom")
    record.step_states["s1"] = StepRunState(step_id="s1", state=StepState.RUNNING)

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(workflow_journal, "settle_step", _boom)
    settle = record_step_completion(_env("run-settleboom", "s1"))
    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.script_runner"):
        settle("term-1", None, "done")  # must not raise

    assert record.step_states["s1"].state == StepState.COMPLETED
    assert [r for r in caplog.records if "completion write failed" in r.getMessage()]


def test_a_begin_step_failure_does_not_fail_the_running_step(monkeypatch: pytest.MonkeyPatch):
    """End to end: the journal is down and the step still completes (INV-4)."""
    _script_record("run-livefail")

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(workflow_journal, "begin_step", _boom)
    hook = make_step_terminal_recorder(_env("run-livefail", "s1"))
    result = _drive(hook, provider="kiro_cli", agent="dev", prompt="go")
    assert result.status == TerminalStatus.COMPLETED
    assert result.last_message == "the answer"


# ---------------------------------------------------------------------------
# INV-1 / INV-3 — the integration unit 6 could not write
# ---------------------------------------------------------------------------
def test_a_full_drive_settles_completed_with_a_readable_envelope():
    """FR-4 guard 1, end to end, from production code for the first time.

    Unit 6's ``begin_step``/``settle_step`` guarantee had only ever been asserted against those
    functions directly. This unit is their first caller, so this is where the guarantee stops
    being theoretical: a settled row carries a readable envelope, and the fingerprint
    ``begin_step`` recorded survives the settle (unit 6 BR-9 — ``settle_step`` never writes
    that column).
    """
    from cli_agent_orchestrator.services.step_result import parse_envelope

    record = _script_record("run-e2e")
    hook = make_step_terminal_recorder(_env("run-e2e", "s1"))
    result = _drive(hook, provider="kiro_cli", agent="dev", prompt="go")

    begun = workflow_journal.get_step("run-e2e", "s1")
    assert begun is not None and begun.state == "running"
    fingerprint_at_begin = begun.call_fingerprint

    settle = record_step_completion(_env("run-e2e", "s1"))
    settle(result.terminal_id, None, result.last_message)

    row = workflow_journal.get_step("run-e2e", "s1")
    assert row is not None
    assert row.state == "completed"
    assert row.result_json is not None  # a settled row and an absent envelope cannot coexist
    envelope = parse_envelope(row.result_json)
    assert envelope is not None
    assert envelope.last_message == "the answer"
    assert envelope.terminal_id == result.terminal_id
    assert row.call_fingerprint == fingerprint_at_begin
    assert record.step_states["s1"].state == StepState.COMPLETED


def test_a_crash_between_begin_and_settle_leaves_the_row_running():
    """FR-12/INV-2: an interrupted step is visible as ``running``, never as ``completed``.

    Simulated by driving the step and never settling — which is exactly what a subprocess
    death in the execution window looks like from the journal's side.
    """
    _script_record("run-crash")
    hook = make_step_terminal_recorder(_env("run-crash", "s1"))
    _drive(hook, provider="kiro_cli", agent="dev", prompt="go")

    row = workflow_journal.get_step("run-crash", "s1")
    assert row is not None
    assert row.state == "running"
    assert row.result_json is None
    assert scheme_of(row.call_fingerprint) == "v2"
