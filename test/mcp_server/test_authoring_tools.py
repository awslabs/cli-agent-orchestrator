"""The four conversational-authoring MCP tools (issue #583 Bolt 3, ``authoring-mcp-tools``).

This unit is the surface **FR-10's Pass criterion is actually about** — "an agent can carry a workflow
from description to running without the user choosing a format".

Four carry the unit's load:

* ``test_no_mcp_tool_grants_an_approval`` — a PERMANENT ABSENCE, and the first tool-inventory assertion in
  this suite. Bolt 2 made ``workflow_plan_approval`` read-only because an MCP grant tool lets an agent
  approve the plan it just wrote; adding four authoring tools is exactly when someone would complete the
  set. Until now the absence rested on convention.
* ``test_authoring_tools_declare_no_field_defaults`` — keeps the ``FieldInfo`` sentinel IMPOSSIBLE rather
  than merely absent. A ``Field(...)`` in a parameter's default arrives as a truthy sentinel when a Python
  caller omits the argument, which is how this very file calls these tools.
* ``test_create_and_update_classify_their_shared_409_differently`` — the discrimination the CLI could not
  make. Both are 409s; already-exists says "pick another name", stale-hash says "re-read and re-apply".
  Mapping from the status alone forces them together.
* ``test_every_tool_reports_an_unreachable_server_as_such`` — asserted against ALL FOUR rather than a
  representative one. Four near-identical thin clients is the shape where a fix lands only on the tool
  someone was looking at, which is the defect PR #650's review found on unit 1's two start arms.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
import requests

from cli_agent_orchestrator.mcp_server import server as S

_TOOLS = ("workflow_create", "workflow_update", "workflow_get", "workflow_validate")

# Minimal valid arguments per tool, so a caller-shape test does not depend on behaviour.
_ARGS = {
    "workflow_create": {"name": "wf", "source": "def main():\n    return {}\n"},
    "workflow_update": {
        "name": "wf",
        "source": "def main():\n    return {}\n",
        "expected_hash": "sha256:old",
    },
    "workflow_get": {"name": "wf"},
    "workflow_validate": {"source": "def main():\n    return {}\n"},
}


def _fn(name: str):
    """The underlying function, whether or not fastmcp wrapped it."""
    tool = getattr(S, name)
    return getattr(tool, "fn", tool)


def _resp(status_code: int, payload=None, *, text: str = ""):
    return SimpleNamespace(
        status_code=status_code,
        json=lambda: (payload if payload is not None else {}),
        text=text,
    )


# ---------------------------------------------------------------------------
# The two invariants
# ---------------------------------------------------------------------------


def test_no_mcp_tool_grants_an_approval():
    """A PERMANENT ABSENCE, enforced rather than remembered.

    An MCP grant tool would let an agent approve the plan it just wrote, collapsing the human
    authorisation FR-8 exists to preserve. Approval stays a human act through ``cao workflow approve``.
    ``workflow_plan_approval`` is deliberately READ-ONLY and is therefore allowed here.
    """
    granting = []
    for attr in dir(S):
        lowered = attr.lower()
        if not lowered.startswith("workflow"):
            continue
        if attr == "workflow_plan_approval":
            continue  # read-only reporter, not a grant
        if any(verb in lowered for verb in ("approve", "grant", "authorise", "authorize")):
            granting.append(attr)

    assert not granting, (
        f"these look like approval-granting MCP tools: {granting}. There must be no grant tool on this "
        "surface, permanently: an agent that can approve the plan it just wrote collapses the human "
        "authorisation FR-8 exists to preserve. Approval is a human act via `cao workflow approve`."
    )


@pytest.mark.parametrize("name", _TOOLS)
def test_authoring_tools_declare_no_field_defaults(name):
    """Keeps the ``FieldInfo`` sentinel impossible, not merely absent.

    The trap: a ``Field(...)`` in a parameter's DEFAULT is passed through as a truthy ``FieldInfo`` object
    when a Python caller omits the argument — and this module's tools are called directly as plain
    functions by the suite. A guard written ``if value:`` then treats "nothing supplied" as "something
    supplied". It is mitigated inline three times elsewhere in this module; here nothing is optional, so it
    cannot arise. This test is what stops a later convenience argument reintroducing it.
    """
    sig = inspect.signature(_fn(name))
    assert sig.parameters, f"{name} takes no parameters?"
    for param, spec in sig.parameters.items():
        assert spec.default is inspect.Parameter.empty, (
            f"{name}({param}=...) has a default. Document a required parameter with "
            "Annotated[str, Field(description=...)] instead — a Field in the DEFAULT reintroduces the "
            "FieldInfo sentinel this unit is otherwise immune to."
        )


def test_omitting_a_required_argument_raises_rather_than_passing_a_sentinel():
    """The property the test above protects, demonstrated end to end."""
    with pytest.raises(TypeError):
        _fn("workflow_create")()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# The shared shape, asserted against every tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _TOOLS)
@pytest.mark.asyncio
async def test_every_tool_reports_an_unreachable_server_as_such(name, monkeypatch):
    """REL-3/REL-7: start the server vs fix the spec are different actions.

    Parametrised over all four deliberately — a transport handler present in three tools and missing in
    the fourth is the defect this shape exists to prevent.
    """

    def boom(*a, **k):
        raise requests.RequestException("connection refused")

    for verb in ("get", "post", "put"):
        monkeypatch.setattr(S.requests, verb, boom)

    out = await _fn(name)(**_ARGS[name])

    assert out["ok"] is False
    assert (
        out["class"] == "unreachable"
    ), f"{name} must not fold a transport failure into a content class"
    assert "cao-server" in out["error"]


@pytest.mark.parametrize("name", _TOOLS)
@pytest.mark.asyncio
async def test_every_tool_survives_a_non_json_body(name, monkeypatch):
    """REL-4: a malformed body degrades to a classed dict rather than raising into the agent loop."""

    def bad_json(*a, **k):
        return SimpleNamespace(
            status_code=500,
            json=lambda: (_ for _ in ()).throw(ValueError("not json")),
            text="<html>oops</html>",
        )

    for verb in ("get", "post", "put"):
        monkeypatch.setattr(S.requests, verb, bad_json)

    out = await _fn(name)(**_ARGS[name])

    assert out["ok"] is False and "class" in out and out["error"]


# ---------------------------------------------------------------------------
# Per-tool behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_sends_source_text_and_returns_the_hash(monkeypatch):
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"], seen["json"] = url, json
        return _resp(201, {"name": "wf", "path": "/x/wf.py", "content_hash": "sha256:aa"})

    monkeypatch.setattr(S.requests, "post", fake_post)

    out = await _fn("workflow_create")(**_ARGS["workflow_create"])

    assert out == {"ok": True, "name": "wf", "path": "/x/wf.py", "content_hash": "sha256:aa"}
    assert seen["json"] == _ARGS["workflow_create"], "the TEXT is sent; the server owns placement"
    assert seen["url"].endswith("/workflows")


@pytest.mark.asyncio
async def test_update_passes_the_hash_verbatim(monkeypatch):
    """SEC-4: transported, never obtained. The tool must not fetch the hash it is about to compare."""
    seen = {}

    def fake_put(url, json=None, timeout=None):
        seen["json"] = json
        return _resp(200, {"name": "wf", "path": "/x/wf.py", "content_hash": "sha256:new"})

    def no_get(*a, **k):
        raise AssertionError("workflow_update must not GET anything — that would defeat the check")

    monkeypatch.setattr(S.requests, "put", fake_put)
    monkeypatch.setattr(S.requests, "get", no_get)

    out = await _fn("workflow_update")(**_ARGS["workflow_update"])

    assert out["content_hash"] == "sha256:new", "the NEW hash comes back for the next edit"
    assert seen["json"]["expected_hash"] == "sha256:old", "verbatim, unnormalised"


@pytest.mark.asyncio
async def test_create_and_update_classify_their_shared_409_differently(monkeypatch):
    """The discrimination knowing the CALLING TOOL buys.

    Both are 409s. Unit 2's CLI mapped classes from the status alone and had to fold them into one
    ``conflict``; here the tool identity separates the two remedies that differ most.
    """
    monkeypatch.setattr(S.requests, "post", lambda *a, **k: _resp(409, {"detail": "exists"}))
    monkeypatch.setattr(S.requests, "put", lambda *a, **k: _resp(409, {"detail": "stale"}))

    created = await _fn("workflow_create")(**_ARGS["workflow_create"])
    updated = await _fn("workflow_update")(**_ARGS["workflow_update"])

    assert created["class"] == "already_exists"
    assert updated["class"] == "stale_hash"
    assert (
        created["class"] != updated["class"]
    ), "one status, two remedies: pick another name vs re-read and re-apply"


@pytest.mark.asyncio
async def test_update_and_get_report_a_missing_spec_as_not_found(monkeypatch):
    monkeypatch.setattr(S.requests, "put", lambda *a, **k: _resp(404, {"detail": "unknown"}))
    monkeypatch.setattr(S.requests, "get", lambda *a, **k: _resp(404, {"detail": "unknown"}))

    assert (await _fn("workflow_update")(**_ARGS["workflow_update"]))["class"] == "not_found"
    assert (await _fn("workflow_get")(**_ARGS["workflow_get"]))["class"] == "not_found"


@pytest.mark.asyncio
async def test_validate_sends_source_and_never_a_path(monkeypatch):
    """REL-6: a draft is checked without existing anywhere, and no path is ever sent."""
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"], seen["json"] = url, json
        return _resp(200, {"status": "pass", "findings": []})

    monkeypatch.setattr(S.requests, "post", fake_post)

    out = await _fn("workflow_validate")(**_ARGS["workflow_validate"])

    assert out["ok"] is True and out["status"] == "pass"
    assert set(seen["json"]) == {"source"}, "a path must never be sent from this tool"
    assert seen["url"].endswith("/workflows/validate")


@pytest.mark.asyncio
async def test_a_success_body_is_spread_verbatim(monkeypatch):
    """BR-5/REL-5: the agent and the CLI must see the same server truth."""
    body = {"name": "wf", "mode": "script", "steps": [], "content_hash": "sha256:cc", "extra": 1}
    monkeypatch.setattr(S.requests, "get", lambda *a, **k: _resp(200, body))

    out = await _fn("workflow_get")(**_ARGS["workflow_get"])

    assert out == {"ok": True, **body}, "no reshaping, no filtering, no renaming"


# ---------------------------------------------------------------------------
# The descriptions are functional artifacts
# ---------------------------------------------------------------------------


def test_update_forbids_the_fetch_then_write_shortcut_in_its_description():
    """BR-7: the description is machine-read metadata, so this is a requirement rather than prose.

    An agent can trivially chain get-then-write, and a hash read from the file about to be overwritten
    always matches — so the chained version does not weaken FR-8's check, it removes it. The warning has to
    be where the agent reads.
    """
    doc = (_fn("workflow_update").__doc__ or "").lower()
    assert "do not fetch it immediately before writing" in doc
    assert "always matches" in doc, "the description must say WHY, not just forbid it"
    assert "workflow_get" in doc, "it must also say where the hash legitimately comes from"


def test_create_tells_the_agent_to_keep_the_hash():
    doc = (_fn("workflow_create").__doc__ or "").lower()
    assert "content_hash" in doc and "workflow_update" in doc


def test_validate_states_the_validate_then_create_order():
    doc = (_fn("workflow_validate").__doc__ or "").lower()
    assert "before it exists" in doc or "validate, revise" in doc
    assert "refuse to run" in doc, "a failing lint must be framed as work to do, not as advice"
