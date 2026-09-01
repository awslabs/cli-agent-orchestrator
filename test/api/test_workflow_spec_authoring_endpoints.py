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

import ast
import hashlib
import inspect
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services import workflow_spec_service as svc

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


def test_warning_findings_survive_create_and_update(client, spec_dir, monkeypatch):
    """Successful authoring must preserve the service's typed warning findings."""
    from cli_agent_orchestrator.models.workflow import LintFinding, ScriptValidationResult

    findings = [
        LintFinding(rule_id="dynamic-import", severity="warning", line=1, message="careful")
    ]

    def _warn_only(source: str, path: str) -> ScriptValidationResult:
        return ScriptValidationResult(status="pass", findings=findings, errors=[])

    monkeypatch.setattr(svc, "lint_script", _warn_only)
    expected = [finding.model_dump() for finding in findings]

    created = client.post("/workflows", json={"name": "warned", "source": GOOD})
    assert created.status_code == 201, created.text
    assert created.json().get("findings") == expected

    updated = client.put(
        "/workflows/warned",
        json={"source": GOOD_V2, "expected_hash": created.json()["content_hash"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json().get("findings") == expected


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


def test_later_update_file_disappearance_is_a_storage_failure_not_unknown_workflow(
    client, spec_dir, monkeypatch
):
    """Only the admission check's dedicated absence result is a 404."""
    created = client.post("/workflows", json={"name": "vanished", "source": GOOD}).json()

    def _vanished(*args: object, **kwargs: object) -> str:
        raise FileNotFoundError(f"gone after admission: {spec_dir / 'vanished.py'}")

    monkeypatch.setattr(svc, "_current_source_hash", _vanished)
    response = client.put(
        "/workflows/vanished",
        json={"source": GOOD_V2, "expected_hash": created["content_hash"]},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "message": "workflow storage failed; retry later or contact the server administrator."
    }
    assert str(spec_dir) not in str(response.json())


def test_authoring_validation_errors_do_not_disclose_server_paths(client, spec_dir):
    """The symlink refusal contains an absolute path internally, never in the 400 body."""
    create_target = spec_dir / "leak-create.py"
    create_target.symlink_to(spec_dir / "missing-target.py")

    create_response = client.post("/workflows", json={"name": "leak-create", "source": GOOD})
    assert create_response.status_code == 400
    assert create_response.json()["detail"] == (
        "workflow specification is invalid; correct the request and try again."
    )
    assert str(spec_dir) not in str(create_response.json())

    backing = spec_dir / "backing.py"
    backing.write_text(GOOD)
    update_target = spec_dir / "leak-update.py"
    update_target.symlink_to(backing)
    update_response = client.put(
        "/workflows/leak-update",
        json={
            "source": GOOD_V2,
            "expected_hash": hashlib.sha256(GOOD.encode("utf-8")).hexdigest(),
        },
    )
    assert update_response.status_code == 400
    assert update_response.json()["detail"] == (
        "workflow specification is invalid; correct the request and try again."
    )
    assert str(spec_dir) not in str(update_response.json())


@pytest.mark.parametrize("operation", ["create", "update"])
def test_storage_failures_are_structured_and_path_free(spec_dir, monkeypatch, operation):
    """Filesystem errors never fall through to Starlette's plain-text 500."""
    client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)

    if operation == "create":

        def _storage_failure(*args: object, **kwargs: object) -> object:
            raise OSError(f"disk full at {spec_dir}")

        monkeypatch.setattr(svc, "create_workflow", _storage_failure)
        response = client.post("/workflows", json={"name": "storage-create", "source": GOOD})
    else:
        created = client.post("/workflows", json={"name": "storage-update", "source": GOOD}).json()

        def _storage_failure(*args: object, **kwargs: object) -> object:
            raise OSError(f"permission denied at {spec_dir}")

        monkeypatch.setattr(svc, "update_workflow", _storage_failure)
        response = client.put(
            "/workflows/storage-update",
            json={"source": GOOD_V2, "expected_hash": created["content_hash"]},
        )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == {
        "message": "workflow storage failed; retry later or contact the server administrator."
    }
    assert str(spec_dir) not in str(response.json())


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
    assert "missing-recovery-policy" in resp.json()["detail"]
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


def test_a_yaml_name_is_refused_without_disclosing_internal_validation_detail(client, spec_dir):
    """A path-free validation message remains actionable at the HTTP boundary."""
    resp = client.post("/workflows", json={"name": "as-yaml.yaml", "source": GOOD})

    assert resp.status_code == 400, resp.text
    assert "YAML specs cannot be created or updated" in resp.json()["detail"]
    assert str(spec_dir) not in resp.json()["detail"]
    assert not any(spec_dir.iterdir()), "nothing may be written for a refused tier"


def test_an_oversize_spec_is_refused_with_the_stable_public_error(client, spec_dir):
    """The write-cap refusal retains the actionable configured byte limit."""
    from cli_agent_orchestrator.constants import WORKFLOW_MAX_SPEC_BYTES

    oversize = "# " + ("x" * (WORKFLOW_MAX_SPEC_BYTES + 10)) + "\ndef main():\n    return {}\n"

    resp = client.post("/workflows", json={"name": "huge", "source": oversize})

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == f"spec exceeds {WORKFLOW_MAX_SPEC_BYTES} bytes (max)"
    assert str(spec_dir) not in resp.json()["detail"]
    assert not any(spec_dir.iterdir()), "the cap is checked BEFORE any file is created"


def test_source_validation_over_cap_is_a_fail_result(client):
    """The source and path forms share the validation-result HTTP contract."""
    from cli_agent_orchestrator.constants import WORKFLOW_MAX_SPEC_BYTES

    response = client.post(
        "/workflows/validate",
        json={"source": "x" * (WORKFLOW_MAX_SPEC_BYTES + 1)},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "fail"


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
                if method == "PUT":
                    doc = getattr(getattr(route, "endpoint", None), "__doc__", "") or ""
                    assert "optimistic concurrency control" in doc
                    assert "not an authorization control" in doc
    assert seen == wanted, f"both authoring routes must be registered; found {seen}"


def test_transient_kinds_are_constructed_by_runtime_step_emitters():
    """The classifier may only classify durable kinds that production can emit."""
    from cli_agent_orchestrator import api
    from cli_agent_orchestrator.services import agent_step

    tree = ast.parse(inspect.getsource(agent_step))
    emitted_kinds = {
        keyword.value.value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "StepExecutionError"
        for keyword in call.keywords
        if keyword.arg == "kind"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    }

    assert api.main._TRANSIENT_ERROR_KINDS <= emitted_kinds
