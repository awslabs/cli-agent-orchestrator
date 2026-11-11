"""A profile's ``params`` block through the profile routes.

``GET /agents/profiles/{name}`` returns ``model_dump(exclude_none=True)``, so the block
appears with no route change, in the shape contract section 1 gives the builder. The
write and validate routes hold it to the same rules as the model.

``POST /agents/profiles/validate`` also returns the block it read, in the same shape, or
null (profile params editor contract, section 1a).
"""

from __future__ import annotations

import pytest

PROFILE = (
    "---\n"
    "name: orders-agent\n"
    "description: Reads the orders table\n"
    "params:\n"
    "  table_name:\n"
    "    description: DynamoDB table name\n"
    "    required: true\n"
    "  partition_key_type:\n"
    "    description: DynamoDB key type\n"
    "    required: true\n"
    "    enum: [S, N, B]\n"
    "  limit:\n"
    "    description: Max items to return\n"
    "    type: int\n"
    "    default: 1\n"
    "  dry_run:\n"
    "    type: bool\n"
    "    default: false\n"
    "---\n\nRead the orders table.\n"
)

# PROFILE's block as both routes return it.
PARAMS = {
    "table_name": {"type": "string", "required": True, "description": "DynamoDB table name"},
    "partition_key_type": {
        "type": "string",
        "required": True,
        "description": "DynamoDB key type",
        "enum": ["S", "N", "B"],
    },
    "limit": {"type": "int", "required": False, "default": 1, "description": "Max items to return"},
    "dry_run": {"type": "bool", "required": False, "default": False},
}


@pytest.fixture()
def write_store(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import profile_store
    from cli_agent_orchestrator.utils import agent_profiles

    target = tmp_path / "agent-store"
    monkeypatch.setattr(profile_store, "LOCAL_AGENT_STORE_DIR", target)
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", target)
    return target


def test_the_detail_route_returns_the_declarations(client, write_store) -> None:
    assert (
        client.post(
            "/agents/profiles", json={"name": "orders-agent", "content": PROFILE}
        ).status_code
        == 201
    )

    body = client.get("/agents/profiles/orders-agent").json()

    assert body["params"] == {
        "table_name": {"type": "string", "required": True, "description": "DynamoDB table name"},
        "partition_key_type": {
            "type": "string",
            "required": True,
            "description": "DynamoDB key type",
            "enum": ["S", "N", "B"],
        },
        "limit": {
            "type": "int",
            "required": False,
            "default": 1,
            "description": "Max items to return",
        },
        "dry_run": {"type": "bool", "required": False, "default": False},
    }
    assert list(body["params"]) == ["table_name", "partition_key_type", "limit", "dry_run"]


def test_a_profile_without_params_has_no_params_key(client, write_store) -> None:
    content = "---\nname: plain\ndescription: d\n---\n\nBody.\n"
    client.post("/agents/profiles", json={"name": "plain", "content": content})

    assert "params" not in client.get("/agents/profiles/plain").json()


def test_validate_accepts_the_declarations(client) -> None:
    resp = client.post("/agents/profiles/validate", json={"content": PROFILE})

    assert resp.json() == {"valid": True, "messages": [], "params": PARAMS}


def test_validate_returns_the_params_the_detail_route_returns(client, write_store) -> None:
    """Same entries, same order, and the same keys in each entry: the model's dump."""
    created = client.post("/agents/profiles", json={"name": "orders-agent", "content": PROFILE})
    assert created.status_code == 201
    detail = client.get("/agents/profiles/orders-agent").json()["params"]

    validated = client.post("/agents/profiles/validate", json={"content": PROFILE}).json()

    assert validated["params"] == detail
    assert list(validated["params"]) == list(detail)
    assert [list(entry) for entry in validated["params"].values()] == [
        list(entry) for entry in detail.values()
    ]
    # The text writes description first. The model's field order puts type and required
    # first, so the entry is the model's dump, not the parsed YAML.
    assert list(validated["params"]["table_name"]) == ["type", "required", "description"]


def test_validate_returns_an_empty_block_as_an_empty_mapping(client) -> None:
    content = "---\nname: orders-agent\ndescription: d\nparams: {}\n---\n\nBody.\n"

    resp = client.post("/agents/profiles/validate", json={"content": content})

    assert resp.json() == {"valid": True, "messages": [], "params": {}}


def test_validate_returns_null_params_without_a_block(client) -> None:
    content = "---\nname: plain\ndescription: d\n---\n\nBody.\n"

    resp = client.post("/agents/profiles/validate", json={"content": content})

    assert resp.json() == {"valid": True, "messages": [], "params": None}


@pytest.mark.parametrize(
    "block,path",
    [
        ("  t:\n    required: true\n    default: orders\n", "params.t"),
        ("  t:\n    enum: [S, N]\n    default: B\n", "params.t.default"),
        ('  t:\n    default: "orders-{{run_id}}"\n', "params.t.default"),
        ('  t:\n    enum: [S, "{{kind}}"]\n', "params.t.enum.1"),
        ("  1t: {}\n", "params"),
    ],
    ids=[
        "required with default",
        "default outside enum",
        "placeholder default",
        "placeholder enum value",
        "bad name",
    ],
)
def test_validate_returns_null_params_for_a_block_with_errors(client, block, path) -> None:
    content = f"---\nname: orders-agent\ndescription: d\nparams:\n{block}---\n\nBody.\n"

    body = client.post("/agents/profiles/validate", json={"content": content}).json()

    assert body["valid"] is False
    assert body["params"] is None
    assert any(m["severity"] == "error" and m["path"] == path for m in body["messages"])


def test_validate_returns_the_params_when_another_key_has_an_error(client) -> None:
    content = PROFILE.replace("params:\n", "allowedTools: [1]\nparams:\n", 1)

    body = client.post("/agents/profiles/validate", json={"content": content}).json()

    assert body["valid"] is False
    assert [m["path"] for m in body["messages"] if m["severity"] == "error"] == ["allowedTools.0"]
    assert body["params"] == PARAMS


def test_a_dollar_placeholder_in_a_description_comes_back_unchanged(client) -> None:
    """The route validates the text as submitted, with no ``${VAR}`` resolution."""
    content = (
        "---\nname: orders-agent\ndescription: d\nparams:\n"
        '  table_name:\n    description: "Fills ${TABLE_NAME}"\n    required: true\n'
        "---\n\nRead ${TABLE_NAME}.\n"
    )

    body = client.post("/agents/profiles/validate", json={"content": content}).json()

    assert body["params"] == {
        "table_name": {"type": "string", "required": True, "description": "Fills ${TABLE_NAME}"}
    }


def _bomb(levels: int = 40) -> str:
    lines = ["hooks:", "  a0: &a0 {k: v}"]
    for level in range(1, levels + 1):
        lines.append(f"  a{level}: &a{level} {{x: *a{level - 1}, y: *a{level - 1}}}")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    "content",
    [
        PROFILE.replace("params:\n", _bomb() + "params:\n", 1),
        "---\nname: bomb\ndescription: d\n"
        + _bomb()
        + "params:\n  t:\n    enum: *a40\n---\n\nBody.\n",
    ],
    ids=["beside a valid block", "inside the block"],
)
def test_a_document_past_the_structural_ceiling_gets_null_params_and_a_small_response(
    client, content
) -> None:
    assert len(content) < 2500

    resp = client.post("/agents/profiles/validate", json={"content": content})

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["params"] is None
    assert any("renders to more than" in m["message"] for m in body["messages"])
    assert len(resp.content) < 2000, f"a {len(resp.content)}-byte response"


def _aliased_enum_profile() -> str:
    """A valid profile whose 32 params all reuse one long enum through a YAML alias.

    The enum holds 32 values of 512 four-byte characters each, so the request is about
    66 KB, and the block would be about 2.1 MB as JSON.
    """
    values = ", ".join(f'"{chr(0x1F600) * 509}{i:03d}"' for i in range(32))
    entries = [f"  p0:\n    enum: &shared [{values}]\n"]
    entries += [f"  p{i}:\n    enum: *shared\n" for i in range(1, 32)]
    return (
        "---\nname: orders-agent\ndescription: d\nparams:\n" + "".join(entries) + "---\n\nBody.\n"
    )


def test_an_aliased_block_does_not_make_the_response_larger_than_the_request(client) -> None:
    content = _aliased_enum_profile()

    resp = client.post("/agents/profiles/validate", json={"content": content})

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["params"] is None
    assert len(resp.content) < len(content.encode("utf-8"))


def test_the_params_cap_counts_the_block_as_json(monkeypatch) -> None:
    import json

    from cli_agent_orchestrator.services import profile_validator

    size = len(json.dumps(PARAMS, ensure_ascii=False).encode("utf-8"))

    monkeypatch.setattr(profile_validator, "_MAX_PARAMS_RESPONSE_BYTES", size)
    at_the_cap = profile_validator.validate_profile_text_with_params(PROFILE).params
    monkeypatch.setattr(profile_validator, "_MAX_PARAMS_RESPONSE_BYTES", size - 1)
    over_the_cap = profile_validator.validate_profile_text_with_params(PROFILE).params

    assert at_the_cap is not None
    assert {name: decl.model_dump(exclude_none=True) for name, decl in at_the_cap.items()} == PARAMS
    assert over_the_cap is None


@pytest.mark.parametrize(
    "block,path",
    [
        ("  t:\n    required: true\n    default: orders\n", "params.t"),
        ("  t:\n    enum: [S, N]\n    default: B\n", "params.t.default"),
        ("  t:\n    pattern: '^o'\n", "params.t"),
        ("  1t: {}\n", "params"),
    ],
    ids=["required with default", "default outside enum", "pattern key", "bad name"],
)
def test_a_broken_declaration_is_refused_and_not_written(client, write_store, block, path) -> None:
    content = f"---\nname: orders-agent\ndescription: d\nparams:\n{block}---\n\nBody.\n"

    validated = client.post("/agents/profiles/validate", json={"content": content}).json()
    created = client.post("/agents/profiles", json={"name": "orders-agent", "content": content})

    assert validated["valid"] is False
    assert any(m["path"] == path for m in validated["messages"])
    assert created.status_code == 400
    assert not (write_store / "orders-agent.md").exists()
