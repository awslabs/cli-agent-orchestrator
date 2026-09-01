"""FR-10's two Pass criteria and FR-8's remaining half, as one scenario (issue #583 Bolt 3).

Unit ``authoring-sequence-proof``. FR-10 says: *consistent agent-facing operations to create, inspect,
validate and update Python workflows, behind a single describe → author → validate → present plan →
approve → run → observe sequence*, and it passes only when *an agent can carry a workflow from
description to running without the user choosing a format, and transient failures are distinguishable
from artifact defects requiring a new run.*

That is a claim about a SEQUENCE. Every other unit in this Bolt verified one link, and each of them can
pass while the sequence is broken — which is not hypothetical here, it already happened. Unit
``authoring-sequence`` found that ``authoring-mcp-tools``' four tools and ``failure-classification``'s
classifier were individually correct while the ``plan_id`` an agent needs to get from one to the next
never reached the wire at all. Every test was green.

``units-generation`` gives the reason this is its own unit, drawn from the previous Bolt: the
upgrade-window test had to be bolted onto Bolt 1B's Definition of Done at ``delivery-planning`` because
``units-generation`` had assigned it to no unit, "despite it being the top-ranked risk". **An end-to-end
scenario owned by nobody does not get written.** Two precedents exist for the same reason:
``replay-verification-guard`` (Bolt 1) and ``frozen-context-proof`` (Bolt 2).

WHAT THIS PROVES, AND WHAT IT DOES NOT
--------------------------------------
It drives the path an AGENT drives: the four MCP tools, with their ``requests`` calls routed into a
FastAPI ``TestClient`` over the real app. Real throughout — the four authoring tools, the three gated
routes, ``workflow_spec_service``'s write path with its containment and tier-collision checks, the real
journal on a temporary database, the real ``approval_store``, the real classifier, and both ``detail``
readers.

**Exactly one boundary is stubbed: the script subprocess.** So this does NOT prove that a real provider
starts, that tmux delivers bytes, or that a real process death preserves the record.

That limit is written here rather than left implicit, for the reason ``frozen-context-proof`` gives: a
module named as an end-to-end proof invites a reader to believe it covers more than it does — and this
issue's Bolt 1C spent an entire unit removing five surfaces that claimed more than they delivered.

WHY THE SUBPROCESS IS STUBBED RATHER THAN REAL. A real subprocess is exercised only under ``test/e2e``,
and CI ignores ``test/e2e`` (``ci.yml``:178-184, ``--ignore=test/e2e -m "not e2e"``). A maximally faithful
version placed there would run when someone remembered to run it by hand — the "owned by nobody" failure
wearing a different hat. ``frozen-context-proof`` settled it: **a proof that does not run is not a
proof.**
"""

from __future__ import annotations

import json

# Reused, not re-invented (TSD-4): a second fake-process shape in one repo diverges from the first.
from test.services.test_script_runner import _FakeProcess, _install_fake_spawn

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.mcp_server import server as mcp
from cli_agent_orchestrator.services import (
    approval_store,
    script_runner,
    settings_service,
    workflow_journal,
    workflow_spec_service,
)

CLEAN_SOURCE = '''"""summarize — a workflow an agent would author."""
from cao_workflow import step, emit_output

handle = step(
    provider="claude_code",
    agent="reviewer",
    prompt="Summarize the input. Return the summary only.",
    step_id="summarize:one",
    recovery="idempotent",
)
emit_output({"summary": handle.output})
'''

UNRUNNABLE_SOURCE = '''"""A draft with a blocking lint error: step() without recovery=."""
from cao_workflow import step

step(provider="claude_code", agent="dev", prompt="go", step_id="s1")
'''


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def agent_world(tmp_path, monkeypatch):
    """The real app, the real stores, a disposable spec dir — and the gate genuinely ON.

    ``WORKFLOW_SPEC_DIR`` points at ``tmp_path`` (SEC-5). MEASURED rather than assumed: a spec created
    there satisfies ``_safe_spec_path``'s containment check, resolves by stem, and carries a hash. Four
    existing modules point it under the real ``Path.home()`` and never clean up — 120 uuid directories
    have accumulated in this developer's home — so this unit deliberately does not add a fifth.

    THE APPROVAL GATE IS REAL AND ON (SEC-1). Never patched, never configured off: the sequence under
    proof exists BECAUSE of FR-8, so a scenario that disabled the gate to make the chain flow would
    demonstrate the opposite of the requirement while showing green. Enforcement is switched on through
    the real setting, the way ``approval-enforcement-default`` established.
    """
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database as database_client

    db = tmp_path / "cao.db"
    monkeypatch.setattr(constants, "DATABASE_FILE", db)
    monkeypatch.setattr(database_client, "DATABASE_FILE", db, raising=False)
    monkeypatch.setattr(workflow_journal, "DATABASE_FILE", db, raising=False)
    monkeypatch.setattr(approval_store, "DATABASE_FILE", db, raising=False)
    database_client.init_db()

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"workflow": {"require_approval": True}}))
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings)
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)

    specs = tmp_path / "workflows"
    specs.mkdir()
    monkeypatch.setattr(workflow_spec_service, "WORKFLOW_SPEC_DIR", specs, raising=True)

    client = TestClient(app, base_url="http://localhost")

    # The requests -> TestClient adapter (FIX-001, TSD-3). TRANSPORT ONLY: it translates the path and
    # the json body and swallows `timeout=`, which a TestClient has no use for. It must never return a
    # body of its own (SEC-2) — the whole value of driving the agent's path is that the request reaches
    # the real route and the real service beneath it, and an adapter that answered would leave this
    # module passing while proving nothing.
    # Patched as seen from mcp_server.server rather than globally: narrower is safer, and a global patch
    # would also capture the shim's callbacks if the fake process ever grew any.
    def _path_of(url: str) -> str:
        return url.split("localhost:8000", 1)[-1] if "localhost:8000" in url else url

    def _get(url, **kw):
        return client.get(_path_of(url))

    def _post(url, json=None, **kw):
        return client.post(_path_of(url), json=json)

    def _put(url, json=None, **kw):
        return client.put(_path_of(url), json=json)

    monkeypatch.setattr(mcp.requests, "get", _get)
    monkeypatch.setattr(mcp.requests, "post", _post)
    monkeypatch.setattr(mcp.requests, "put", _put)

    return client


def _tool(name: str):
    """The underlying function, whether or not fastmcp wrapped it."""
    obj = getattr(mcp, name)
    return getattr(obj, "fn", obj)


def _approve_as_a_human_would(client: TestClient, plan_id: str) -> dict:
    """Grant the approval through the real route — the only sanctioned path.

    NOT by disabling the gate and NOT by writing to the store directly. ``cao workflow approve`` is what
    a human runs, and this is the request it makes. There is deliberately no MCP tool that does this: an
    agent that could approve the plan it just wrote would make the gate decorative in exactly the case it
    was designed for.
    """
    response = client.post("/workflows/plans/approve", json={"plan_id": plan_id})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# CLAIM 1 — no format question CAN be asked (FR-10 Pass, format)
# ---------------------------------------------------------------------------


def test_no_authoring_surface_accepts_a_format_parameter():
    """FR-10's Fail condition is *the user is asked to choose YAML or Python*.

    ASSERTED AS AN ABSENT CAPABILITY, not an absent behaviour, and that distinction is what makes the
    criterion testable at all. "The sequence did not ask the user to choose a format" passes trivially
    and FOREVER, because a test never answers a question — it would be a green assertion with no teeth
    from the day it was written. The only way this criterion can actually regress is someone adding a
    ``--format`` option or a ``language=`` field, and THIS is the assertion that fails then.
    """
    import inspect

    from cli_agent_orchestrator.api.main import (
        WorkflowCreateRequest,
        WorkflowUpdateRequest,
        WorkflowValidateRequest,
    )

    banned = {"format", "language", "tier", "kind", "filetype", "extension"}

    for tool_name in ("workflow_create", "workflow_update", "workflow_validate"):
        params = set(inspect.signature(_tool(tool_name)).parameters)
        assert not (params & banned), (
            f"{tool_name} accepts {params & banned}: FR-10 fails if the user can be asked to choose a "
            "format. Python is the generated format and there is no question to ask."
        )

    for model in (WorkflowCreateRequest, WorkflowUpdateRequest, WorkflowValidateRequest):
        fields = set(model.model_fields)
        assert not (fields & banned), f"{model.__name__} exposes {fields & banned}"


def test_the_cli_authoring_verbs_offer_no_format_option():
    """The human surface, for the same reason — and ``--from-file`` is not a format choice."""
    from cli_agent_orchestrator.cli.commands.workflow import create_cmd, update_cmd

    for command in (create_cmd, update_cmd):
        names = {opt.name for opt in command.params}
        assert not (
            names & {"format", "language", "tier", "extension"}
        ), f"{command.name} exposes a format choice: {names}"


# ---------------------------------------------------------------------------
# CLAIM 2 — the chain completes, and the middle link is load-bearing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_carries_a_workflow_from_a_draft_to_a_completed_run(
    agent_world, monkeypatch
):
    """THE SCENARIO. Every step is a call an agent makes, in order, against the real app.

    What makes this a proof rather than six unit tests concatenated: **the ``plan_id`` passed to the
    approve step is the one READ OUT OF THE REFUSAL**, never one the test computed. That is the link
    that did not exist before this Bolt's ``authoring-sequence`` unit — a scenario that computed the
    identifier would pass even against code whose refusal carried nothing at all, which is precisely
    the state every unit test was green in.
    """
    client = agent_world

    # ── b. VALIDATE a draft that exists nowhere ──────────────────────────────
    verdict = await _tool("workflow_validate")(source=CLEAN_SOURCE)
    assert verdict["ok"] is True, verdict
    assert verdict["status"] == "pass", verdict

    # ── a. AUTHOR through the create path (never a raw file write) ────────────
    created = await _tool("workflow_create")(name="summarize", source=CLEAN_SOURCE)
    assert created["ok"] is True, created
    assert created["content_hash"], "the hash comes back so a later update is a one-liner"

    # ── d. PRESENT THE PLAN: the first run of a new plan is refused BY DESIGN ─
    proc = _FakeProcess(exit_rc=0, stdout=b'CAO_WORKFLOW_OUTPUT:{"summary": "ok"}\n')
    _install_fake_spawn(monkeypatch, proc)
    monkeypatch.setattr(script_runner, "_reconcile_orphans", _noop_sweep)

    # EVERY argument is passed explicitly, including ``inputs``, and that is NOT tidiness — it is a
    # workaround for a live defect this proof uncovered. ``workflow_run``, ``workflow_start`` and
    # ``workflow_resume`` declare EVERY parameter with a ``Field(...)`` DEFAULT, so a direct Python call
    # that omits one passes a ``FieldInfo`` object through, and it reaches ``json.dumps`` as
    # ``TypeError: Object of type FieldInfo is not JSON serializable``. Through a real MCP client
    # fastmcp fills the defaults, so an agent does not see it — but the sentinel is exactly the trap
    # ``authoring-mcp-tools`` designed itself out of with ``Annotated``, and these three are among the
    # eleven tools that audit has not reached (standing task #18). Reported rather than fixed here: this
    # unit changes no source (BR-8).
    refused = await _tool("workflow_run")(name_or_path="summarize", inputs={}, run_id="proof-1")
    assert refused["ok"] is False, "a brand-new plan must not run unapproved"
    assert refused["class"] == "approval_required", refused
    plan_id = refused["plan_id"]
    assert plan_id, "the identifier must reach the agent as a FIELD — this is the link under proof"

    # ── e. APPROVE — a human act, through the real route, with THAT identifier ─
    record = _approve_as_a_human_would(client, plan_id)
    assert record["plan_id"] == plan_id, "byte-identical: no normalisation on either leg"

    # ── f. RUN: the SAME plan, now permitted ─────────────────────────────────
    # Nothing between the two runs changes an execution-affecting field, so the identifier is
    # unchanged and the approval just granted is the one that applies (BR-3). Re-authoring here would
    # mint a different plan_id and prove nothing.
    ran = await _tool("workflow_run")(name_or_path="summarize", inputs={}, run_id="proof-2")

    assert ran["ok"] is True, f"the approved plan must run: {ran}"
    assert ran["state"] == "completed", ran


@pytest.mark.asyncio
async def test_the_refusal_carries_the_identifier_the_approve_route_accepts(
    agent_world, monkeypatch
):
    """The seam, isolated: the value from the refusal is accepted verbatim by the approve route.

    Separated from the scenario above so a failure names its own cause. If the scenario breaks, this
    says whether the seam or a later step is at fault.
    """
    client = agent_world
    await _tool("workflow_create")(name="seam", source=CLEAN_SOURCE)
    _install_fake_spawn(monkeypatch, _FakeProcess(exit_rc=0))
    monkeypatch.setattr(script_runner, "_reconcile_orphans", _noop_sweep)

    refused = await _tool("workflow_run")(name_or_path="seam", inputs={}, run_id="seam-1")
    plan_id = refused["plan_id"]

    assert approval_store.is_approved(plan_id) is False, "not approved before"
    _approve_as_a_human_would(client, plan_id)
    assert approval_store.is_approved(plan_id) is True, (
        "the identifier the agent was handed must be the one the store now recognises — a "
        "normalisation on either leg would break exactly this"
    )


@pytest.mark.asyncio
async def test_a_draft_with_a_lint_error_never_becomes_a_spec(agent_world):
    """VALIDATE is a real gate, not advice: a blocking finding stops the chain at step b."""
    verdict = await _tool("workflow_validate")(source=UNRUNNABLE_SOURCE)

    assert verdict["ok"] is True, "the request succeeded; the VERDICT is the failure"
    assert verdict["status"] == "fail", verdict
    assert verdict["findings"], "the findings are the actionable part"

    refused = await _tool("workflow_create")(name="broken", source=UNRUNNABLE_SOURCE)
    assert refused["ok"] is False, "the write path lints too — it must refuse to persist this"


# ---------------------------------------------------------------------------
# CLAIM 3 — the two failure classes are distinguishable BY THE AGENT
# ---------------------------------------------------------------------------


def _failed_run(run_id: str, error_kind: str) -> None:
    """A terminal FAILED run whose step carries a durable ``error_kind``.

    ``update_step`` is the writer that projects ``error_kind`` into the durable column — ``settle_step``
    has no such parameter, a wrong-precedent slip that cost a cycle at ``failure-classification``.
    """
    workflow_journal.insert_run(
        run_id,
        "summarize",
        json.dumps({"source": "x", "path": None, "content_hash": "sha256:abc"}),
        "{}",
        "failed",
        "2026-08-25T00:00:00+00:00",
        "script",
        "1",
        None,
    )
    workflow_journal.insert_steps(run_id, [("s1", "running")], "2026-08-25T00:00:00+00:00")
    workflow_journal.update_step(
        run_id,
        "s1",
        "failed",
        1,  # attempts — a POSITIONAL between state and updated_at; omitting it is a TypeError
        "2026-08-25T00:00:01+00:00",
        error="boom",
        error_kind=error_kind,
    )


@pytest.mark.asyncio
async def test_the_agent_can_tell_a_transient_failure_from_an_artifact_defect(agent_world):
    """FR-10's SECOND Pass criterion, at the surface the criterion is about.

    ASSERTED AS A PAIR, never one at a time: either alone permits the collapsed implementation where
    every failure returns the same class, which is exactly the conflation FR-10's Fail condition names.
    The discipline comes from ``test_the_two_causes_do_not_share_a_status_on_the_resume_arm``.

    WHAT THIS ADDS OVER ``failure-classification``'s OWN TESTS, which already assert the pair at the HTTP
    route: it asserts the field survives the ``workflow_result`` TOOL's envelope. That tool's description
    instructs an agent to branch on ``failure_envelope.classification`` — so if the envelope dropped the
    field, the instruction would be false and nothing else would notice. The two runs differ ONLY in
    their persisted ``error_kind``.
    """
    _failed_run("proof-transient", "timeout")
    _failed_run("proof-defect", "lint_error")

    transient = await _tool("workflow_result")(run_id="proof-transient")
    defect = await _tool("workflow_result")(run_id="proof-defect")

    assert transient["ok"] is True, transient
    assert defect["ok"] is True, defect

    t_class = transient["failure_envelope"]["classification"]
    d_class = defect["failure_envelope"]["classification"]

    assert t_class == "transient", f"a timeout is resumable: {transient['failure_envelope']}"
    assert (
        d_class == "artifact_defect"
    ), f"a lint error needs a new run: {defect['failure_envelope']}"
    assert t_class != d_class, (
        "the two classes must not collapse — an agent that cannot tell them apart will advise a "
        "resume for a failure that will fail identically every time"
    )


# ---------------------------------------------------------------------------
# CLAIM 4 — FR-8 completes: a stale hash is rejected, through the agent's surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_update_presenting_a_stale_hash_is_rejected(agent_world):
    """The criterion Bolt 2's Definition of Done had to declare unsatisfied.

    THE HASH IS MADE STALE BY AN INTERVENING REAL WRITE, not fabricated. A made-up string would prove
    only that the comparison rejects garbage — which pass 3A's unit tests already establish. The claim
    here is that a real write invalidates a real prior read, which is what FR-8 is actually about.
    """
    created = await _tool("workflow_create")(name="drifting", source=CLEAN_SOURCE)
    first_hash = created["content_hash"]

    # Someone else writes. The agent's hash is now stale — genuinely, not notionally.
    second = await _tool("workflow_update")(
        name="drifting",
        source=CLEAN_SOURCE + "\n# an intervening edit\n",
        expected_hash=first_hash,
    )
    assert (
        second["ok"] is True
    ), f"the first update holds the current hash, so it succeeds: {second}"
    assert second["content_hash"] != first_hash, "the write moved the hash"

    stale = await _tool("workflow_update")(
        name="drifting",
        source=CLEAN_SOURCE + "\n# a second edit from a stale read\n",
        expected_hash=first_hash,  # the hash from BEFORE the intervening write
    )

    assert stale["ok"] is False, "an update from a stale read must be refused"
    assert (
        stale["class"] == "stale_hash"
    ), f"and it must be distinguishable from an already-exists conflict: {stale}"


@pytest.mark.asyncio
async def test_the_hash_an_agent_reads_back_is_the_one_update_accepts(agent_world):
    """``workflow_get`` → ``workflow_update`` is the sanctioned recovery from a stale hash, so it has
    to work — otherwise the refusal above would be a dead end."""
    await _tool("workflow_create")(name="recover", source=CLEAN_SOURCE)
    fetched = await _tool("workflow_get")(name="recover")
    assert fetched["ok"] is True, fetched

    updated = await _tool("workflow_update")(
        name="recover",
        source=CLEAN_SOURCE + "\n# fixed\n",
        expected_hash=fetched["content_hash"],
    )

    assert updated["ok"] is True, f"a freshly-read hash must be accepted: {updated}"


async def _noop_sweep(run_id):
    """``_reconcile_orphans`` does a journal sweep this scenario has no use for."""
    return None
