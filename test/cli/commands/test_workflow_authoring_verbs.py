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

import json as _json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.workflow import workflow

GOOD = 'def main():\n    return {"ok": True}\n'
SECRET_ISH = 'TOKEN = "hunter2-do-not-echo"\n\n\ndef main():\n    return {"ok": True}\n'


def _resp(status_code: int, payload):
    """A minimal stand-in for requests.Response — only what the verbs read."""
    return SimpleNamespace(
        status_code=status_code,
        json=lambda: payload,
        text=_json.dumps(payload),
    )


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
    "status,expected_class",
    [(400, "invalid_request"), (404, "not_found"), (409, "conflict"), (422, "lint_failed")],
)
def test_each_refusal_carries_its_class_in_json_mode(
    monkeypatch, source_file, status, expected_class
):
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.workflow.requests.post",
        lambda url, json=None, timeout=None: _resp(status, {"detail": "the server's own words"}),
    )

    result = CliRunner().invoke(
        workflow, ["create", "authored", "--from-file", str(source_file), "--json"]
    )

    assert result.exit_code == 1, "exit stays uniform; the class rides the envelope"
    payload = _json.loads(result.output.replace("Error: ", "", 1))
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

    payload = _json.loads(result.output.replace("Error: ", "", 1))
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
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.workflow.requests.get",
        lambda url, timeout=None: _resp(
            200, {"name": "s", "mode": "script", "steps": [], "content_hash": "sha256:cc"}
        ),
    )

    result = CliRunner().invoke(workflow, ["get", "s"])

    assert result.exit_code == 0, result.output
    assert (
        "Hash:" in result.output and "sha256:cc" in result.output
    ), "this was the only place a human could find the value `update` requires"


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
