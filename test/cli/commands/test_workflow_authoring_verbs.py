"""``cao workflow create`` / ``update``, and the ``Hash:`` line (issue #583 Bolt 3).

Three carry the unit's load:

* ``test_update_requires_expected_hash`` — the flag is required and there is no ``--force``. A CLI that
  computed the hash from the file about to be overwritten would not weaken FR-8's stale-update check,
  it would REMOVE it: such a hash always matches.
* ``test_get_shows_the_hash_for_a_script_spec`` / ``..._omits_it_for_yaml`` — the pair. Either alone
  permits the wrong implementation: asserting only presence permits an unconditional ``Hash: None`` on
  every YAML spec, and asserting only absence permits never printing it at all.
* ``test_the_source_is_never_echoed`` — a spec may carry a credential the operator pasted by mistake,
  and terminal output is the easiest place for that to leak.
"""

from __future__ import annotations

import asyncio
import json as _json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.workflow import workflow
from cli_agent_orchestrator.mcp_server import server as mcp_server
from cli_agent_orchestrator.models.workflow import ScriptSpec

GOOD = 'def main():\n    return {"ok": True}\n'
SECRET_ISH = 'TOKEN = "hunter2-do-not-echo"\n\n\ndef main():\n    return {"ok": True}\n'


def _resp(status_code: int, payload):
    """A minimal stand-in for requests.Response — only what the verbs read."""
    return SimpleNamespace(
        status_code=status_code,
        json=lambda: payload,
        text=_json.dumps(payload),
    )


def _mcp_tool(name: str):
    tool = getattr(mcp_server, name)
    return getattr(tool, "fn", tool)


@pytest.fixture()
def source_file(tmp_path):
    p = tmp_path / "draft.py"
    p.write_text(GOOD)
    return p


def test_create_posts_the_source_text_and_reports_the_hash(monkeypatch, source_file):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _resp(
            201, {"name": "authored", "path": "/x/authored.py", "content_hash": "sha256:aa"}
        )

    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.workflow.requests.post", fake_post)

    result = CliRunner().invoke(workflow, ["create", "authored", "--from-file", str(source_file)])

    assert result.exit_code == 0, result.output
    assert captured["json"] == {
        "name": "authored",
        "source": GOOD,
    }, "the CONTENTS are sent, never the path — the server decides where a spec lands"
    assert "sha256:aa" in result.output, "the hash is what `update` needs next"


@pytest.mark.parametrize("verb", ("create", "update"))
def test_authoring_json_success_envelopes_include_ok(monkeypatch, source_file, verb):
    response = _resp(201 if verb == "create" else 200, {"name": "wf", "content_hash": "sha256:new"})
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.workflow.requests."
        + ("post" if verb == "create" else "put"),
        lambda *args, **kwargs: response,
    )
    arguments = (
        [verb, "wf", "--from-file", str(source_file), "--json"]
        if verb == "create"
        else [
            verb,
            "wf",
            "--from-file",
            str(source_file),
            "--expected-hash",
            "sha256:old",
            "--json",
        ]
    )

    result = CliRunner().invoke(workflow, arguments)

    assert result.exit_code == 0, result.output
    assert _json.loads(result.output)["ok"] is True


def test_create_requires_from_file(monkeypatch):
    def unreachable(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("no request may be issued when a required option is missing")

    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.workflow.requests.post", unreachable)

    result = CliRunner().invoke(workflow, ["create", "authored"])

    assert result.exit_code != 0
    assert "--from-file" in result.output


def test_update_requires_expected_hash(monkeypatch, source_file):
    """No flag, no default, no --force. The assertion IS the mechanism."""

    def unreachable(*a, **k):  # pragma: no cover
        raise AssertionError("no request may be issued without an asserted hash")

    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.workflow.requests.put", unreachable)

    result = CliRunner().invoke(workflow, ["update", "wf", "--from-file", str(source_file)])

    assert result.exit_code != 0
    assert "--expected-hash" in result.output
    assert (
        "--force" not in result.output
    ), "an unguarded update path that exists is a path that gets used"


def test_update_passes_the_hash_through_verbatim(monkeypatch, source_file):
    captured = {}

    def fake_put(url, json=None, timeout=None):
        captured["json"] = json
        return _resp(200, {"name": "wf", "path": "/x/wf.py", "content_hash": "sha256:new"})

    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.workflow.requests.put", fake_put)

    result = CliRunner().invoke(
        workflow,
        ["update", "wf", "--from-file", str(source_file), "--expected-hash", "sha256:old"],
    )

    assert result.exit_code == 0, result.output
    assert captured["json"]["expected_hash"] == "sha256:old", "never recomputed, never normalised"
    assert "sha256:new" in result.output, "the NEW hash comes back, closing the loop"


@pytest.mark.parametrize(
    "verb,status,expected_class",
    [
        ("create", 400, "invalid_request"),
        ("create", 409, "already_exists"),
        ("create", 422, "error"),
        ("update", 404, "not_found"),
        ("update", 409, "stale_hash"),
    ],
)
def test_each_refusal_carries_its_class_in_json_mode(
    monkeypatch, source_file, verb, status, expected_class
):
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.workflow.requests."
        + ("post" if verb == "create" else "put"),
        lambda url, json=None, timeout=None: _resp(status, {"detail": "the server's own words"}),
    )

    arguments = (
        [verb, "authored", "--from-file", str(source_file), "--json"]
        if verb == "create"
        else [
            verb,
            "authored",
            "--from-file",
            str(source_file),
            "--expected-hash",
            "sha256:old",
            "--json",
        ]
    )
    result = CliRunner().invoke(workflow, arguments)

    assert result.exit_code == 1, "exit stays uniform; the class rides the envelope"
    payload = _json.loads(result.output)
    assert payload["ok"] is False
    assert payload["class"] == expected_class
    assert payload["message"] == "the server's own words", (
        "the server's message is surfaced verbatim — the over-cap refusal names the actual limit and "
        "the YAML refusal names the restriction, so replacing it throws away the actionable part"
    )


def test_a_transport_failure_is_not_a_refusal(monkeypatch, source_file):
    import requests as _requests

    def boom(*a, **k):
        raise _requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.workflow.requests.post", boom)

    result = CliRunner().invoke(
        workflow, ["create", "authored", "--from-file", str(source_file), "--json"]
    )

    payload = _json.loads(result.output)
    assert payload["class"] == "unreachable", (
        "start the server versus fix the spec are different actions, so this cannot be folded into "
        "the refusal classes"
    )


def test_the_source_is_never_echoed(monkeypatch, tmp_path):
    src = tmp_path / "secretish.py"
    src.write_text(SECRET_ISH)
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.workflow.requests.post",
        lambda url, json=None, timeout=None: _resp(
            201, {"name": "s", "path": "/x/s.py", "content_hash": "sha256:bb"}
        ),
    )

    result = CliRunner().invoke(workflow, ["create", "s", "--from-file", str(src)])

    assert result.exit_code == 0
    assert "hunter2-do-not-echo" not in result.output


def test_get_shows_the_hash_for_a_script_spec(monkeypatch):
    script = ScriptSpec(
        name="s",
        path="/x/s.py",
        source=GOOD,
        content_hash="sha256:cc",
    ).model_dump()
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.workflow.requests.get",
        lambda url, timeout=None: _resp(200, script),
    )

    result = CliRunner().invoke(workflow, ["get", "s"])

    assert result.exit_code == 0, result.output
    assert (
        "Hash:" in result.output and "sha256:cc" in result.output
    ), "this was the only place a human could find the value `update` requires"
    assert "Mode:        script" in result.output


def test_get_omits_the_hash_for_yaml(monkeypatch):
    """The other half of the pair: a YAML spec carries no hash, so the line must be absent."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.workflow.requests.get",
        lambda url, timeout=None: _resp(200, {"name": "y", "mode": "sequential", "steps": []}),
    )

    result = CliRunner().invoke(workflow, ["get", "y"])

    assert result.exit_code == 0, result.output
    assert (
        "Hash:" not in result.output
    ), "printing 'Hash: None' on every YAML spec would be noise that reads like a defect"


@pytest.mark.parametrize("tool_name", ("workflow_create", "workflow_update", "workflow_get"))
def test_mcp_authoring_rejects_path_like_names_before_any_request(monkeypatch, tool_name):
    def request_must_not_happen(
        *args, **kwargs
    ):  # pragma: no cover - assertion proves the boundary
        raise AssertionError("invalid names must never reach the HTTP URL builder")

    monkeypatch.setattr(mcp_server.requests, "post", request_must_not_happen)
    monkeypatch.setattr(mcp_server.requests, "put", request_must_not_happen)
    monkeypatch.setattr(mcp_server.requests, "get", request_must_not_happen)
    tool = _mcp_tool(tool_name)
    kwargs = {"name": "../workflows"}
    if tool_name == "workflow_create":
        kwargs["source"] = GOOD
    elif tool_name == "workflow_update":
        kwargs.update(source=GOOD, expected_hash="sha256:old")

    result = asyncio.run(tool(**kwargs))

    assert result["ok"] is False
    assert result["class"] == "invalid_request"


@pytest.mark.parametrize(
    "tool_name", ("workflow_create", "workflow_update", "workflow_get", "workflow_validate")
)
def test_mcp_authoring_converts_non_mapping_success_bodies_to_refusals(monkeypatch, tool_name):
    response = _resp(201 if tool_name == "workflow_create" else 200, ["not", "an", "object"])
    monkeypatch.setattr(mcp_server.requests, "post", lambda *args, **kwargs: response)
    monkeypatch.setattr(mcp_server.requests, "put", lambda *args, **kwargs: response)
    monkeypatch.setattr(mcp_server.requests, "get", lambda *args, **kwargs: response)
    tool = _mcp_tool(tool_name)
    kwargs = {"name": "workflow"}
    if tool_name == "workflow_create":
        kwargs["source"] = GOOD
    elif tool_name == "workflow_update":
        kwargs.update(source=GOOD, expected_hash="sha256:old")
    elif tool_name == "workflow_validate":
        kwargs = {"source": GOOD}

    result = asyncio.run(tool(**kwargs))

    assert result == {
        "ok": False,
        "class": "error",
        "error": "cao-server returned an unexpected success response",
    }


def test_mcp_get_reports_a_tier_collision_as_a_conflict(monkeypatch):
    monkeypatch.setattr(
        mcp_server.requests,
        "get",
        lambda *args, **kwargs: _resp(409, {"detail": "workflow tiers collide"}),
    )

    result = asyncio.run(_mcp_tool("workflow_get")(name="workflow"))

    assert result["ok"] is False
    assert result["class"] == "conflict"
    assert result["error"] == "workflow tiers collide"


@pytest.mark.parametrize("verb,arguments", [("run", ["workflow"]), ("resume", ["run-1"])])
def test_json_approval_refusals_match_the_mcp_envelope(monkeypatch, verb, arguments):
    plan_id = "plan-v1:abc"
    response = _resp(
        403,
        {
            "detail": {
                "kind": "approval_required",
                "plan_id": plan_id,
                "message": "Plan must be approved.",
            }
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.workflow.requests.post",
        lambda *args, **kwargs: response,
    )

    result = CliRunner().invoke(workflow, [verb, *arguments, "--json"])

    assert result.exit_code == 1
    assert _json.loads(result.output) == {
        "ok": False,
        "class": "approval_required",
        "plan_id": plan_id,
        "error": "Plan must be approved.",
    }
