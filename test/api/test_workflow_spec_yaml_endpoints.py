"""YAML specs through the existing authoring routes (the workflow builder's save path).

``POST /workflows``, ``PUT /workflows/{name}`` and ``POST /workflows/validate`` take an optional
``tier`` (``"python"`` by default). ``GET /workflows/{name}`` returns ``path``, ``source`` and
``content_hash`` for a YAML spec. These tests drive the HTTP surface, because the transport and
its status mapping are the deliverable: the service functions are tested on their own in
``test/services/test_workflow_spec_yaml_create_update.py``.

Two properties carry the Python tier through unchanged, and are asserted here rather than
assumed: a request without ``tier`` (every CLI verb and MCP tool sends none) gets exactly the old
response shape, and a Python spec's GET body has exactly the old keys.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.constants import WORKFLOW_MAX_SPEC_BYTES
from cli_agent_orchestrator.models.workflow import ScriptSpec, WorkflowSpec

PYTHON_GOOD = 'def main():\n    """A spec that lints clean."""\n    return {"ok": True}\n'


def _spec_yaml(name: str, prompt: str = "do it", *, mode: str = "sequential") -> str:
    """A spec in the builder's style: every key and string value JSON-quoted."""
    return (
        f'"name": "{name}"\n'
        '"description": "a builder spec"\n'
        f'"mode": "{mode}"\n'
        '"inputs":\n'
        '  "run_label": {"type": "string", "required": true}\n'
        '"steps":\n'
        '  - "id": "only-step"\n'
        '    "provider": "claude_code"\n'
        '    "agent": "developer"\n'
        f'    "prompt": "{prompt}"\n'
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_YAML_WRITE_KEYS = {"name", "path", "content_hash", "findings", "status", "reserved_notes"}
_PYTHON_WRITE_KEYS = {"name", "path", "content_hash", "findings"}


@pytest.fixture()
def spec_dir(monkeypatch: pytest.MonkeyPatch):
    """Isolate ``WORKFLOW_SPEC_DIR``; the routes take no caller-supplied destination."""
    base = Path.home() / ".cao-test-wf-yaml-api" / uuid.uuid4().hex
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


def _create(client: TestClient, name: str, source: str):
    return client.post("/workflows", json={"name": name, "source": source, "tier": "yaml"})


def _entries(base: Path) -> list[str]:
    return sorted(p.name for p in base.iterdir())


# ---------------------------------------------------------------------------
# POST /workflows, tier "yaml"
# ---------------------------------------------------------------------------
def test_yaml_create_writes_name_yaml_and_returns_the_contract_shape(client, spec_dir):
    source = _spec_yaml("built")

    resp = _create(client, "built", source)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == _YAML_WRITE_KEYS
    assert body["name"] == "built"
    assert body["findings"] == []
    assert body["status"] == "pass"
    assert body["reserved_notes"] == []
    on_disk = (spec_dir / "built.yaml").read_bytes()
    assert on_disk == source.encode("utf-8")
    assert body["content_hash"] == _sha(on_disk)
    assert _entries(spec_dir) == ["built.yaml"], "no temp file left behind"


def test_yaml_create_reports_a_reserved_construct(client, spec_dir):
    resp = _create(client, "later", _spec_yaml("later", mode="parallel"))

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pass_reserved"
    assert resp.json()["reserved_notes"] == [
        "construct 'parallel' is reserved (not built yet; implemented by unit N7)"
    ]


@pytest.mark.parametrize("body_tier", [None, "python"])
def test_python_create_response_is_unchanged(client, spec_dir, body_tier):
    """No tier (what the CLI and MCP tools send) and tier "python" give the old response."""
    payload = {"name": f"py-{body_tier}", "source": PYTHON_GOOD}
    if body_tier is not None:
        payload["tier"] = body_tier

    resp = client.post("/workflows", json=payload)

    assert resp.status_code == 201, resp.text
    assert set(resp.json()) == _PYTHON_WRITE_KEYS
    assert (spec_dir / f"py-{body_tier}.py").read_text() == PYTHON_GOOD


@pytest.mark.parametrize(
    "name,source,fragment",
    [
        pytest.param("bad name!", _spec_yaml("ok"), "is invalid", id="invalid-name"),
        pytest.param("x.yaml", _spec_yaml("x"), "without the '.yaml' extension", id="ext-yaml"),
        pytest.param("x.yml", _spec_yaml("x"), "without the '.yml' extension", id="ext-yml"),
        pytest.param("x.py", _spec_yaml("x"), "without the '.py' extension", id="ext-py"),
        pytest.param("broken", "name: [unclosed\n", "malformed YAML", id="does-not-parse"),
        pytest.param(
            "agg",
            '"name": "agg"\n"steps":\n'
            '  - {"id": "dup", "provider": "p", "agent": "a", "prompt": "x"}\n'
            '  - {"id": "dup", "provider": "p", "agent": "a", "prompt": "y"}\n'
            '  - {"id": "bad id!", "provider": "p", "agent": "a", "prompt": "z"}\n',
            "duplicate step id 'dup'; step id 'bad id!' is invalid",
            id="grammar-lists-errors",
        ),
        pytest.param("mine", _spec_yaml("other"), "declares name 'other'", id="name-mismatch"),
        pytest.param(
            "huge",
            "#" + "x" * WORKFLOW_MAX_SPEC_BYTES + "\n" + _spec_yaml("huge"),
            f"spec exceeds {WORKFLOW_MAX_SPEC_BYTES} bytes (max)",
            id="over-cap",
        ),
    ],
)
def test_yaml_create_400s_write_nothing(client, spec_dir, name, source, fragment):
    resp = _create(client, name, source)

    assert resp.status_code == 400, resp.text
    assert fragment in resp.json()["detail"]
    assert str(spec_dir) not in resp.json()["detail"]
    assert _entries(spec_dir) == []


@pytest.mark.parametrize("existing", ["taken.yaml", "taken.yml", "taken.py"])
def test_yaml_create_409s_when_the_name_exists_in_any_tier(client, spec_dir, existing):
    original = PYTHON_GOOD if existing.endswith(".py") else _spec_yaml("taken", "original")
    (spec_dir / existing).write_text(original)

    resp = _create(client, "taken", _spec_yaml("taken", "replacement"))

    assert resp.status_code == 409, resp.text
    assert (spec_dir / existing).read_text() == original
    assert _entries(spec_dir) == [existing]


def test_an_unknown_tier_is_a_request_validation_error(client, spec_dir):
    resp = client.post("/workflows", json={"name": "t", "source": PYTHON_GOOD, "tier": "toml"})

    assert resp.status_code == 422
    assert _entries(spec_dir) == []


def test_a_lone_surrogate_yaml_source_is_400(spec_dir):
    client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
    resp = client.post(
        "/workflows",
        content=('{"name": "sur", "tier": "yaml", "source": "\\"name\\": \\"sur\\"\\n# \\ud800"}'),
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 400, resp.text
    assert "UTF-8" in resp.json()["detail"]
    assert _entries(spec_dir) == []


# ---------------------------------------------------------------------------
# GET /workflows/{name}
# ---------------------------------------------------------------------------
def test_get_returns_the_yaml_spec_with_path_source_and_hash(client, spec_dir):
    source = _spec_yaml("shown")
    assert _create(client, "shown", source).status_code == 201

    resp = client.get("/workflows/shown")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == set(WorkflowSpec.model_fields) | {"path", "source", "content_hash"}
    assert body["source"] == source
    assert body["content_hash"] == _sha((spec_dir / "shown.yaml").read_bytes())
    assert body["path"].endswith("shown.yaml")
    assert body["inputs"] == {"run_label": {"type": "string", "required": True, "default": None}}
    assert body["steps"][0]["prompt"] == "do it"


def test_get_of_a_python_spec_is_unchanged(client, spec_dir):
    assert (
        client.post("/workflows", json={"name": "scripted", "source": PYTHON_GOOD}).status_code
        == 201
    )

    resp = client.get("/workflows/scripted")

    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == set(ScriptSpec.model_fields)
    assert resp.json()["source"] == PYTHON_GOOD


def test_list_shows_the_tier_by_extension(client, spec_dir):
    assert _create(client, "listed", _spec_yaml("listed")).status_code == 201

    rows = {row["name"]: row for row in client.get("/workflows").json()}

    assert rows["listed"]["source_path"].endswith("listed.yaml")
    assert rows["listed"]["step_count"] == 1


# ---------------------------------------------------------------------------
# PUT /workflows/{name}, tier "yaml"
# ---------------------------------------------------------------------------
def test_yaml_update_round_trips_the_get_hash(client, spec_dir):
    assert _create(client, "edit-me", _spec_yaml("edit-me", "v1")).status_code == 201
    current = client.get("/workflows/edit-me").json()

    resp = client.put(
        "/workflows/edit-me",
        json={
            "source": _spec_yaml("edit-me", "v2"),
            "expected_hash": current["content_hash"],
            "tier": "yaml",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == _YAML_WRITE_KEYS
    assert body["status"] == "pass"
    assert (spec_dir / "edit-me.yaml").read_text() == _spec_yaml("edit-me", "v2")
    assert body["content_hash"] == client.get("/workflows/edit-me").json()["content_hash"]

    again = client.put(
        "/workflows/edit-me",
        json={
            "source": _spec_yaml("edit-me", "v3"),
            "expected_hash": body["content_hash"],
            "tier": "yaml",
        },
    )
    assert again.status_code == 200, "the returned hash must be usable without another GET"


def test_yaml_update_keeps_a_yml_file_at_its_own_path(client, spec_dir):
    target = spec_dir / "hand.yml"
    target.write_text(_spec_yaml("hand", "v1"))

    resp = client.put(
        "/workflows/hand",
        json={
            "source": _spec_yaml("hand", "v2"),
            "expected_hash": _sha(target.read_bytes()),
            "tier": "yaml",
        },
    )

    assert resp.status_code == 200, resp.text
    assert target.read_text() == _spec_yaml("hand", "v2")
    assert _entries(spec_dir) == ["hand.yml"]


def test_yaml_update_of_an_unknown_name_is_404(client, spec_dir):
    resp = client.put(
        "/workflows/never",
        json={"source": _spec_yaml("never"), "expected_hash": "0" * 64, "tier": "yaml"},
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "unknown workflow 'never'"
    assert _entries(spec_dir) == []


def test_yaml_update_with_a_stale_hash_is_409(client, spec_dir):
    created = _create(client, "raced", _spec_yaml("raced", "v1")).json()
    client.put(
        "/workflows/raced",
        json={
            "source": _spec_yaml("raced", "v2"),
            "expected_hash": created["content_hash"],
            "tier": "yaml",
        },
    )

    resp = client.put(
        "/workflows/raced",
        json={
            "source": _spec_yaml("raced", "v3"),
            "expected_hash": created["content_hash"],
            "tier": "yaml",
        },
    )

    assert resp.status_code == 409, resp.text
    assert (spec_dir / "raced.yaml").read_text() == _spec_yaml("raced", "v2")


def test_yaml_update_of_a_python_workflow_is_409_and_says_so(client, spec_dir):
    created = client.post("/workflows", json={"name": "scripted", "source": PYTHON_GOOD}).json()

    resp = client.put(
        "/workflows/scripted",
        json={
            "source": _spec_yaml("scripted"),
            "expected_hash": created["content_hash"],
            "tier": "yaml",
        },
    )

    assert resp.status_code == 409, resp.text
    assert "is a Python workflow" in resp.json()["detail"]
    assert _entries(spec_dir) == ["scripted.py"]


@pytest.mark.parametrize("sibling", ["both.py", "both.yml"])
def test_yaml_update_with_a_conflicting_sibling_is_409(client, spec_dir, sibling):
    target = spec_dir / "both.yaml"
    target.write_text(_spec_yaml("both", "v1"))
    (spec_dir / sibling).write_text(
        PYTHON_GOOD if sibling.endswith(".py") else _spec_yaml("both", "twin")
    )

    resp = client.put(
        "/workflows/both",
        json={
            "source": _spec_yaml("both", "v2"),
            "expected_hash": _sha(target.read_bytes()),
            "tier": "yaml",
        },
    )

    assert resp.status_code == 409, resp.text
    assert target.read_text() == _spec_yaml("both", "v1")


@pytest.mark.parametrize(
    "source,fragment",
    [
        pytest.param("name: [unclosed\n", "malformed YAML", id="does-not-parse"),
        pytest.param(_spec_yaml("renamed"), "declares name 'renamed'", id="name-mismatch"),
        pytest.param(
            "#" + "x" * WORKFLOW_MAX_SPEC_BYTES + "\n" + _spec_yaml("keep"),
            f"spec exceeds {WORKFLOW_MAX_SPEC_BYTES} bytes (max)",
            id="over-cap",
        ),
    ],
)
def test_yaml_update_400s_leave_the_file(client, spec_dir, source, fragment):
    created = _create(client, "keep", _spec_yaml("keep")).json()

    resp = client.put(
        "/workflows/keep",
        json={"source": source, "expected_hash": created["content_hash"], "tier": "yaml"},
    )

    assert resp.status_code == 400, resp.text
    assert fragment in resp.json()["detail"]
    assert (spec_dir / "keep.yaml").read_text() == _spec_yaml("keep")


def test_python_update_of_a_yaml_workflow_is_409_and_says_so(client, spec_dir):
    """The mirror of the YAML update of a Python workflow (review of #1291).

    It was 404 "unknown workflow" while GET returned the workflow, which left a CLI or
    script caller that edits a builder-made file with no hint.
    """
    created = _create(client, "yaml-only", _spec_yaml("yaml-only")).json()

    resp = client.put(
        "/workflows/yaml-only",
        json={"source": PYTHON_GOOD, "expected_hash": created["content_hash"]},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == (
        "workflow 'yaml-only' is a YAML workflow; update it in the web UI or over HTTP with "
        "tier 'yaml'. The CLI and the MCP tools write Python only."
    )
    assert _entries(spec_dir) == ["yaml-only.yaml"]
    assert (spec_dir / "yaml-only.yaml").read_text() == _spec_yaml("yaml-only")


def test_python_update_of_a_missing_workflow_is_still_404(client, spec_dir):
    resp = client.put("/workflows/absent", json={"source": PYTHON_GOOD, "expected_hash": "0" * 64})

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "unknown workflow 'absent'"


# ---------------------------------------------------------------------------
# POST /workflows/validate
# ---------------------------------------------------------------------------
def test_validate_yaml_source_returns_the_validation_result_shape(client):
    resp = client.post("/workflows/validate", json={"source": _spec_yaml("v"), "tier": "yaml"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "pass", "reserved_notes": [], "errors": []}


def test_validate_yaml_source_reports_reserved_and_failed_verdicts(client):
    reserved = client.post(
        "/workflows/validate", json={"source": _spec_yaml("v", mode="loop"), "tier": "yaml"}
    ).json()
    failed = client.post(
        "/workflows/validate", json={"source": "name: [unclosed\n", "tier": "yaml"}
    ).json()

    assert reserved["status"] == "pass_reserved"
    assert reserved["reserved_notes"] == [
        "construct 'loop' is reserved (not built yet; implemented by unit N8)"
    ]
    assert failed["status"] == "fail"
    assert failed["errors"][0].startswith("malformed YAML")


def test_validate_yaml_source_over_the_cap_fails_with_the_write_message(client):
    resp = client.post(
        "/workflows/validate",
        json={"source": "#" + "x" * WORKFLOW_MAX_SPEC_BYTES, "tier": "yaml"},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "fail",
        "reserved_notes": [],
        "errors": [f"spec exceeds {WORKFLOW_MAX_SPEC_BYTES} bytes (max)"],
    }


def test_validate_yaml_source_writes_nothing(client, spec_dir):
    client.post("/workflows/validate", json={"source": _spec_yaml("v"), "tier": "yaml"})
    assert _entries(spec_dir) == []


@pytest.mark.parametrize("tier", ["yaml", "python"])
def test_tier_with_the_path_form_is_400(client, spec_dir, tier):
    target = spec_dir / "on-disk.yaml"
    target.write_text(_spec_yaml("on-disk"))

    resp = client.post("/workflows/validate", json={"path": str(target), "tier": tier})

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "tier applies only to the source form"


def test_python_source_validate_is_the_same_with_or_without_tier(client):
    without = client.post("/workflows/validate", json={"source": PYTHON_GOOD})
    explicit = client.post("/workflows/validate", json={"source": PYTHON_GOOD, "tier": "python"})

    assert without.status_code == explicit.status_code == 200
    assert without.json() == explicit.json()
    assert without.json()["tier"] == "script", "still the script lint result"


def test_validate_with_an_unknown_tier_is_a_request_validation_error(client):
    resp = client.post("/workflows/validate", json={"source": "x", "tier": "toml"})
    assert resp.status_code == 422


def test_validate_lone_surrogate_yaml_source_is_400():
    client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
    resp = client.post(
        "/workflows/validate",
        content='{"tier": "yaml", "source": "\\ud800"}',
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 400
    assert "UTF-8" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# The step fields (call, params) through the authoring routes
# ---------------------------------------------------------------------------
def _builder_yaml(name: str, *, call: str = "handoff", params: str = "") -> str:
    """What the builder writes: call always present, params only when non-empty."""
    return (
        f'"name": "{name}"\n'
        '"description": ""\n'
        '"mode": "sequential"\n'
        '"inputs": {}\n'
        '"steps":\n'
        '  - "id": "send-message-01"\n'
        '    "provider": "claude_code"\n'
        '    "agent": "sqs-sender-beta"\n'
        f'    "call": "{call}"\n' + params + '    "prompt": "Send message 01."\n'
    )


_PARAMS = (
    '    "params":\n'
    '      "aws-profile-name": "beta"\n'
    '      "queue-name": "example-queue.fifo"\n'
    '      "aws-account-number": "123456789012"\n'
)


def test_get_returns_call_and_params_in_declaration_order(client, spec_dir):
    assert (
        _create(client, "with-params", _builder_yaml("with-params", params=_PARAMS)).status_code
        == 201
    )

    step = client.get("/workflows/with-params").json()["steps"][0]

    assert step["call"] == "handoff"
    assert list(step["params"].items()) == [
        ("aws-profile-name", "beta"),
        ("queue-name", "example-queue.fifo"),
        ("aws-account-number", "123456789012"),
    ]


@pytest.mark.parametrize("call", ["assign", "send_message"])
def test_a_reserved_call_saves_as_pass_reserved(client, spec_dir, call):
    resp = _create(
        client,
        f"later-{call.replace('_', '-')}",
        _builder_yaml(f"later-{call.replace('_', '-')}", call=call),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pass_reserved"
    assert resp.json()["reserved_notes"] == [f"construct '{call}' is reserved (not built yet)"]


def test_bad_params_are_a_400_naming_each_error(client, spec_dir):
    bad = '    "params":\n      "1bad": "x"\n      "acct": 123456789012\n'

    resp = _create(client, "bad-params", _builder_yaml("bad-params", params=bad))

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "step 'send-message-01': param name '1bad' is invalid" in detail
    assert "step 'send-message-01': param 'acct' must be a string, not a number" in detail
    assert _entries(spec_dir) == []


def test_validate_reports_param_errors_as_a_fail_verdict(client):
    bad = '    "params":\n      "k": "line1\\nline2"\n'

    resp = client.post(
        "/workflows/validate", json={"source": _builder_yaml("v", params=bad), "tier": "yaml"}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "fail"
    assert resp.json()["errors"] == [
        "step 'send-message-01': param 'k' must be one line (no line breaks)"
    ]
