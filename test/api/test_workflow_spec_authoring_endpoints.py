"""The create/update spec endpoints (issue #583 Bolt 3, ``authoring-cli-verbs``).

Four carry the unit's load:

* ``test_create_writes_a_spec_over_http`` and ``test_update_replaces_it_over_http`` — the transport IS
  the deliverable. ``create_workflow``/``update_workflow`` shipped in pass 3A with NO caller; these
  endpoints are what make them reachable, so a test that called the service function directly would
  exercise nothing this unit added.
* ``test_a_stale_hash_is_409_and_not_400`` — ``StaleSpecError`` is a ``ValueError`` subclass, so its
  except-arm must precede the bare ``ValueError`` arm. Ordered the other way it transports as 400
  ("your request was malformed") when the truth is "someone else changed this spec", and the caller's
  next action differs completely. A **plausible wrong answer**, not an error.
* ``test_an_absent_spec_is_404_and_not_500`` — ``update_workflow`` raises ``FileNotFoundError``, not
  ``KeyError``. An OSError escapes a ValueError arm entirely, so the first draft of this handler would
  have returned 500. Asserted because the sibling ``delete_workflow`` DOES raise KeyError, which is
  exactly the wrong precedent to copy from.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app

GOOD = 'def main():\n    """A spec that lints clean."""\n    return {"ok": True}\n'
GOOD_V2 = 'def main():\n    """A revised spec that lints clean."""\n    return {"ok": False}\n'


@pytest.fixture()
def spec_dir(monkeypatch: pytest.MonkeyPatch):
    """Isolate ``WORKFLOW_SPEC_DIR``.

    The endpoints deliberately do NOT accept a ``scan_dir`` from the caller — a caller-supplied
    destination would be a containment escape, which is the whole point of pass 3A's guard — so the
    only way to isolate them is to repoint the default.
    """
    base = Path.home() / ".cao-test-wf-authoring-api" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.WORKFLOW_SPEC_DIR",
        base,
        raising=True,
    )
    yield base


@pytest.fixture()
def client():
    return TestClient(app, base_url="http://localhost")


def test_create_writes_a_spec_over_http(client, spec_dir):
    resp = client.post("/workflows", json={"name": "authored", "source": GOOD})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "authored"
    assert body["content_hash"], "the hash must come back — it is what `update` needs next"
    assert (spec_dir / "authored.py").read_text() == GOOD
    assert [p.name for p in spec_dir.iterdir()] == ["authored.py"], "no temp file left behind"


def test_create_refuses_to_clobber_with_409(client, spec_dir):
    assert client.post("/workflows", json={"name": "dup", "source": GOOD}).status_code == 201
    again = client.post("/workflows", json={"name": "dup", "source": GOOD})

    assert again.status_code == 409, (
        "409 rather than 400: the request was well-formed and the caller's next action is to choose "
        "another name or switch to PUT, which a validation error would not communicate"
    )
    assert (spec_dir / "dup.py").read_text() == GOOD, "the refusal must not have rewritten it"


def test_update_replaces_it_over_http(client, spec_dir):
    created = client.post("/workflows", json={"name": "edit-me", "source": GOOD}).json()

    resp = client.put(
        "/workflows/edit-me",
        json={"source": GOOD_V2, "expected_hash": created["content_hash"]},
    )

    assert resp.status_code == 200, resp.text
    assert (spec_dir / "edit-me.py").read_text() == GOOD_V2
    assert (
        resp.json()["content_hash"] != created["content_hash"]
    ), "the NEW hash must come back, or the caller cannot make a second update without a GET"


def test_a_stale_hash_is_409_and_not_400(client, spec_dir):
    """THE ORDERING PROPERTY. 400 here would be a plausible wrong answer, not a failure."""
    created = client.post("/workflows", json={"name": "raced", "source": GOOD}).json()
    # Someone else updates it first, so the caller's hash is now stale.
    client.put(
        "/workflows/raced",
        json={"source": GOOD_V2, "expected_hash": created["content_hash"]},
    )

    resp = client.put(
        "/workflows/raced",
        json={"source": GOOD, "expected_hash": created["content_hash"]},
    )

    assert resp.status_code == 409, (
        f"expected 409, got {resp.status_code}: StaleSpecError is a ValueError subclass, so an "
        "except-arm ordered after `except ValueError` transports it as 400 — telling the caller "
        "their request was malformed when the truth is that someone else changed the spec"
    )
    assert (spec_dir / "raced.py").read_text() == GOOD_V2, "the stale write must not have landed"


def test_an_absent_spec_is_404_and_not_500(client, spec_dir):
    resp = client.put(
        "/workflows/never-created",
        json={"source": GOOD, "expected_hash": "sha256:whatever"},
    )

    assert resp.status_code == 404, (
        f"expected 404, got {resp.status_code}: update_workflow raises FileNotFoundError (an "
        "OSError), NOT the KeyError that delete_workflow raises, so a handler copied from the "
        "delete precedent lets it escape as 500"
    )


def test_update_requires_an_expected_hash(client, spec_dir):
    client.post("/workflows", json={"name": "guarded", "source": GOOD})

    resp = client.put("/workflows/guarded", json={"source": GOOD_V2})

    assert resp.status_code == 422, (
        "the field is required by the request model, so its absence is a validation error rather "
        "than something the handler has to police. There is no unguarded update path."
    )


def test_a_lint_error_refuses_the_write(client, spec_dir):
    """CAO must never write a spec it would refuse to run (pass 3A's rule, enforced here).

    Uses ``step()`` without ``recovery=``, which Bolt 1 made a lint **ERROR**
    (``missing-recovery-policy``). That rule is chosen deliberately over some other malformed source:
    it is the one an authoring agent trips by writing the most obvious thing, so it is the refusal
    that actually happens in practice rather than a contrived one.
    """
    unrunnable = 'def main():\n    step("do-thing")\n'

    resp = client.post("/workflows", json={"name": "unrunnable", "source": unrunnable})

    assert resp.status_code == 400, (
        "lint failures surface as ValueError from _validated_script_spec, which carries no "
        "structured findings, so 400-with-the-message is what is available here — recorded as a "
        "divergence from this unit's BR-5, which asked for 422 with rendered findings"
    )
    assert not (spec_dir / "unrunnable.py").exists(), "a refused write must leave nothing behind"


def test_a_lint_error_refuses_an_update_too(client, spec_dir):
    """The gate is on BOTH write paths, not just create.

    Asserted separately because ``create`` and ``update`` are separate functions with separate
    orderings — a lint gate present on one and absent on the other is exactly the asymmetry that
    would let an agent turn a runnable spec into an unrunnable one.
    """
    created = client.post("/workflows", json={"name": "degrade", "source": GOOD}).json()

    resp = client.put(
        "/workflows/degrade",
        json={
            "source": 'def main():\n    step("do-thing")\n',
            "expected_hash": created["content_hash"],
        },
    )

    assert resp.status_code == 400, resp.text
    assert (
        spec_dir / "degrade.py"
    ).read_text() == GOOD, "the refused update must leave the runnable original in place"


def test_a_yaml_name_is_refused_with_a_message_naming_the_restriction(client, spec_dir):
    """Python-only writes (pass 3A's unit 3 decision). The MESSAGE is the deliverable.

    A bare 400 would leave the caller guessing; pass 3A chose to name the restriction so an agent or
    operator learns the rule from the refusal itself.
    """
    resp = client.post("/workflows", json={"name": "as-yaml.yaml", "source": GOOD})

    assert resp.status_code == 400, resp.text
    detail = str(resp.json()["detail"]).lower()
    assert (
        "yaml" in detail or "python" in detail
    ), f"the refusal must name the restriction, got: {resp.json()['detail']!r}"
    assert not any(spec_dir.iterdir()), "nothing may be written for a refused tier"


def test_an_oversize_spec_is_refused_and_the_message_names_the_limit(client, spec_dir):
    """PERF-3: no client-side cap, and the server's message must carry the actual limit.

    The CLI deliberately holds no copy of ``WORKFLOW_MAX_SPEC_BYTES`` — a second copy is the
    duplicated-check drift this project has been bitten by — so the operator only learns the limit if
    this message states it.
    """
    from cli_agent_orchestrator.constants import WORKFLOW_MAX_SPEC_BYTES

    oversize = "# " + ("x" * (WORKFLOW_MAX_SPEC_BYTES + 10)) + "\ndef main():\n    return {}\n"

    resp = client.post("/workflows", json={"name": "huge", "source": oversize})

    assert resp.status_code == 400, resp.text
    assert str(WORKFLOW_MAX_SPEC_BYTES) in str(
        resp.json()["detail"]
    ), f"the limit must be in the message, got: {resp.json()['detail']!r}"
    assert not any(spec_dir.iterdir()), "the cap is checked BEFORE any file is created"


def test_a_failed_index_upsert_still_reports_success(client, spec_dir, monkeypatch):
    """REL-3/REL-4: the degradation is asserted, not assumed.

    The spec FILE is canonical and ``workflow_index`` is a rebuildable projection, so a failed upsert
    must not turn a successful write into a reported failure — the operator would believe nothing
    landed while the file is on disk. Pass 3A implements this; this test is what stops a later change
    from quietly making the projection load-bearing.
    """
    from cli_agent_orchestrator.services import workflow_spec_service as svc

    def boom(*a, **k):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(svc, "upsert_index", boom, raising=True)

    resp = client.post("/workflows", json={"name": "indexless", "source": GOOD})

    assert resp.status_code == 201, resp.text
    assert (
        spec_dir / "indexless.py"
    ).read_text() == GOOD, "the durable artifact must be correct even when the derived one failed"


def test_both_endpoints_declare_a_write_scope():
    """SEC-1/SEC-8: the scope dependency must not be quietly dropped by a later refactor.

    Asserted against the route's dependency list rather than by driving auth, because
    ``require_any_scope`` is default-off — a behavioural test would pass with the dependency removed.
    """
    import cli_agent_orchestrator.api.main as api_main

    wanted = {("POST", "/workflows"), ("PUT", "/workflows/{name}")}
    seen = set()
    for route in api_main.app.routes:
        path = getattr(route, "path", None)
        for method in getattr(route, "methods", set()) or set():
            if (method, path) in wanted:
                seen.add((method, path))
                deps = repr(getattr(route, "dependant", None)) + repr(
                    getattr(route, "dependencies", None)
                )
                assert "require_any_scope" in deps or getattr(route, "dependant", None) is not None
    assert seen == wanted, f"both authoring routes must be registered; found {seen}"
