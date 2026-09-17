"""The widened validate body: `{source}` alongside `{path}` (issue #583 Bolt 3, ``authoring-mcp-tools``).

Added so an agent can lint a DRAFT before creating it — the validate-then-create order FR-10's sequence
wants. Two properties carry the change:

* ``test_the_path_form_is_unchanged`` — the whole point of widening rather than replacing. Every existing
  caller, including ``cao workflow validate``, sends ``{path}`` and must be byte-identically unaffected.
* ``test_a_source_only_validate_writes_nothing`` — no temp file. ``lint_script`` accepts a placeholder
  path, so materialising the source would add a write and a cleanup path to a read-only route, and would
  create a path this endpoint would then have to contain.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app

CLEAN = 'def main():\n    """Lints clean."""\n    return {"ok": True}\n'
UNRUNNABLE = 'def main():\n    step("do-thing")\n'  # missing recovery= -> a lint ERROR


@pytest.fixture()
def client():
    return TestClient(app, base_url="http://localhost")


@pytest.fixture()
def spec_dir(monkeypatch):
    base = Path.home() / ".cao-test-wf-validate-source" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.WORKFLOW_SPEC_DIR",
        base,
        raising=True,
    )
    yield base


def test_a_clean_draft_validates_without_existing_anywhere(client):
    resp = client.post("/workflows/validate", json={"source": CLEAN})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pass"


def test_a_draft_with_a_lint_error_fails_the_verdict_rather_than_the_request(client):
    """A lint failure is a VERDICT, not an error: the caller asked whether it lints and got an answer."""
    resp = client.post("/workflows/validate", json={"source": UNRUNNABLE})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "fail"
    assert body["findings"], "the findings are the actionable part"


def test_a_source_only_validate_writes_nothing(client, spec_dir):
    """No temp file, no created spec — a read-only route stays read-only."""
    client.post("/workflows/validate", json={"source": CLEAN})
    client.post("/workflows/validate", json={"source": UNRUNNABLE})

    assert (
        list(spec_dir.iterdir()) == []
    ), f"nothing may be written, found {list(spec_dir.iterdir())}"


def test_the_path_form_is_unchanged(client, spec_dir):
    """The reason this is a widening rather than a replacement.

    ``cao workflow validate`` and every other existing caller send ``{path}``. If this regressed, the
    change would have broken a shipped verb to add an agent convenience.
    """
    target = spec_dir / "on-disk.py"
    target.write_text(CLEAN)

    resp = client.post("/workflows/validate", json={"path": str(target)})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pass"


@pytest.mark.parametrize(
    "body,why",
    [
        ({}, "neither form supplied"),
        ({"path": "/tmp/x.py", "source": CLEAN}, "both forms supplied"),
    ],
)
def test_exactly_one_form_is_required(client, body, why):
    """Enforced rather than defaulted: silently preferring one form would make a both-supplied request
    look as though it validated the thing the caller meant."""
    resp = client.post("/workflows/validate", json=body)

    assert resp.status_code == 400, f"{why} must be refused: {resp.text}"
    detail = str(resp.json()["detail"])
    assert "exactly one" in detail and "path" in detail and "source" in detail


def test_an_oversize_draft_returns_a_fail_verdict_and_names_the_limit(client):
    """The same cap the write path enforces, so a caller cannot learn the limit only by trying to create."""
    from cli_agent_orchestrator.constants import WORKFLOW_MAX_SPEC_BYTES

    oversize = "# " + ("x" * (WORKFLOW_MAX_SPEC_BYTES + 10)) + "\ndef main():\n    return {}\n"

    resp = client.post("/workflows/validate", json={"source": oversize})

    assert resp.status_code == 200
    assert resp.json()["status"] == "fail"
    assert any(
        str(WORKFLOW_MAX_SPEC_BYTES) in error for error in resp.json()["errors"]
    ), resp.json()["errors"]
