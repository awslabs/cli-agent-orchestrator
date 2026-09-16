"""Credential forwarding across the seven PR699 workflow-authoring clients."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from test.conftest import mint_test_token
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import jwt
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.cli.commands import workflow as cli_workflow
from cli_agent_orchestrator.mcp_server import server as mcp_server
from cli_agent_orchestrator.models.workflow import ScriptSpec, ScriptValidationResult
from cli_agent_orchestrator.security import auth as auth_module
from cli_agent_orchestrator.services import approval_store, script_lint, workflow_spec_service

SOURCE = 'def main():\n    return {"ok": True}\n'
PLAN_ID = "plan-v1:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
INVALID_TOKEN = "credential-forwarding-invalid-sentinel"


@pytest.fixture(autouse=True)
def auth_fixture_compatibility(monkeypatch):
    """Adapt newer shared fixtures without leaking attributes beyond each test."""
    # The shared fixture names ``reset_jwks_cache`` but this pinned PR head
    # exposes the same reset only as ``get_jwks_cache().clear()``.
    if not hasattr(auth_module, "reset_jwks_cache"):
        monkeypatch.setattr(
            auth_module,
            "reset_jwks_cache",
            auth_module.get_jwks_cache().clear,
            raising=False,
        )
    # ``mock_jwks`` patches the legacy requests seam; this head uses
    # PyJWKClient, which ``jwks_boundary`` replaces below.
    if not hasattr(auth_module, "requests"):
        monkeypatch.setattr(
            auth_module,
            "requests",
            SimpleNamespace(get=None),
            raising=False,
        )


def _mcp_tool(name: str):
    tool = getattr(mcp_server, name)
    return getattr(tool, "fn", tool)


@dataclass
class _RequestAdapter:
    client: TestClient
    seen_headers: list[dict[str, str] | None]
    server_auth_env: dict[str, str] | None = None

    @staticmethod
    def _path(url: str) -> str:
        parsed = urlsplit(url)
        return parsed.path or "/"

    @contextmanager
    def _server_environment(self):
        if self.server_auth_env is None:
            yield
            return
        with patch.dict(os.environ, self.server_auth_env):
            yield

    def get(self, url: str, **kwargs: Any):
        headers = kwargs.get("headers")
        self.seen_headers.append(headers)
        with self._server_environment():
            return self.client.get(self._path(url), headers=headers)

    def post(self, url: str, json: Any = None, **kwargs: Any):
        headers = kwargs.get("headers")
        self.seen_headers.append(headers)
        with self._server_environment():
            return self.client.post(self._path(url), headers=headers, json=json)

    def put(self, url: str, json: Any = None, **kwargs: Any):
        headers = kwargs.get("headers")
        self.seen_headers.append(headers)
        with self._server_environment():
            return self.client.put(self._path(url), headers=headers, json=json)


@pytest.fixture
def client_boundary(monkeypatch):
    """Route both real clients into FastAPI while stubbing only persistence/lint work."""
    spec = ScriptSpec(
        name="wf",
        path="/specs/wf.py",
        source=SOURCE,
        content_hash="sha256:test",
    )
    monkeypatch.setattr(workflow_spec_service, "create_workflow", lambda name, source: spec)
    monkeypatch.setattr(
        workflow_spec_service,
        "update_workflow",
        lambda name, source, expected_hash: spec,
    )
    monkeypatch.setattr(workflow_spec_service, "get_workflow", lambda name: spec)
    monkeypatch.setattr(
        script_lint,
        "lint_script",
        lambda source, path: ScriptValidationResult(status="pass", findings=[]),
    )
    monkeypatch.setattr(
        approval_store,
        "get_approval",
        lambda plan_id: approval_store.PlanApproval(
            plan_id=plan_id,
            approved_at="2026-09-16T00:00:00Z",
            approved_by="tester",
        ),
    )

    adapter = _RequestAdapter(
        TestClient(app, base_url="http://localhost"),
        [],
    )
    monkeypatch.setattr(mcp_server.requests, "get", adapter.get)
    monkeypatch.setattr(mcp_server.requests, "post", adapter.post)
    monkeypatch.setattr(mcp_server.requests, "put", adapter.put)
    return adapter


@pytest.fixture
def source_file(tmp_path):
    path = tmp_path / "workflow.py"
    path.write_text(SOURCE)
    return path


@pytest.fixture
def jwks_boundary(mock_jwks, rsa_keys, monkeypatch):
    """Use the shared JWKS fixture, adapted to this head's PyJWKClient implementation."""
    public_key = jwt.PyJWK.from_dict(rsa_keys[1].as_dict())

    class _PyJWKClient:
        def __init__(self, uri: str):
            self.uri = uri

        def get_jwk_set(self):
            return {"keys": [rsa_keys[1].as_dict()]}

        def get_signing_key_from_jwt(self, token: str):
            jwt.get_unverified_header(token)
            return public_key

    monkeypatch.setattr(auth_module, "PyJWKClient", _PyJWKClient)


def _set_token(monkeypatch, rsa_keys, scopes: str) -> str:
    token = mint_test_token(rsa_keys[0], scopes=scopes)
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", token)
    return token


def _split_client_from_authenticated_server(client_boundary, monkeypatch) -> None:
    """Keep server auth on only while the in-process request crosses its boundary."""
    client_boundary.server_auth_env = {
        key: os.environ[key]
        for key in (
            "AUTH0_DOMAIN",
            "AUTH0_AUDIENCE",
            "CAO_AUTH_JWKS_URI",
            "CAO_AUTH_AUDIENCE",
            "CAO_AUTH_ISSUER",
        )
        if key in os.environ
    }
    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("CAO_AUTH_JWKS_URI", raising=False)
    assert auth_module.is_auth_enabled() is False


def _assert_secret_absent(secret: str, *values: object) -> None:
    combined = "\n".join(str(value) for value in values)
    assert secret not in combined


def test_valid_tokens_cross_all_seven_client_boundaries(
    auth_enabled_env,
    jwks_boundary,
    rsa_keys,
    client_boundary,
    source_file,
    monkeypatch,
):
    """Each named client reaches its real route with the minimum accepted scope."""
    _split_client_from_authenticated_server(client_boundary, monkeypatch)
    read_token = _set_token(monkeypatch, rsa_keys, "cao:read")
    assert auth_module.get_local_bearer() is None
    assert auth_module.get_client_bearer() == read_token
    got = asyncio.run(_mcp_tool("workflow_get")(name="wf"))
    validated = asyncio.run(_mcp_tool("workflow_validate")(source=SOURCE))
    assert got["ok"] is True
    assert validated["ok"] is True

    write_token = _set_token(monkeypatch, rsa_keys, "cao:write")
    created = asyncio.run(_mcp_tool("workflow_create")(name="wf", source=SOURCE))
    updated = asyncio.run(
        _mcp_tool("workflow_update")(
            name="wf",
            source=SOURCE,
            expected_hash="sha256:test",
        )
    )
    runner = CliRunner()
    cli_created = runner.invoke(
        cli_workflow.workflow,
        ["create", "wf", "--from-file", str(source_file)],
    )
    cli_updated = runner.invoke(
        cli_workflow.workflow,
        [
            "update",
            "wf",
            "--from-file",
            str(source_file),
            "--expected-hash",
            "sha256:test",
        ],
    )
    assert created["ok"] is True
    assert updated["ok"] is True
    assert cli_created.exit_code == 0, cli_created.output
    assert cli_updated.exit_code == 0, cli_updated.output

    admin_token = _set_token(monkeypatch, rsa_keys, "cao:admin")
    approved = runner.invoke(cli_workflow.workflow, ["approve", PLAN_ID])
    assert approved.exit_code == 0, approved.output

    expected = [
        f"Bearer {read_token}",
        f"Bearer {read_token}",
        f"Bearer {write_token}",
        f"Bearer {write_token}",
        f"Bearer {write_token}",
        f"Bearer {write_token}",
        f"Bearer {admin_token}",
    ]
    assert [headers["Authorization"] for headers in client_boundary.seen_headers] == expected
    _assert_secret_absent(
        read_token,
        got,
        validated,
        created,
        updated,
        cli_created.output,
        cli_updated.output,
        approved.output,
    )
    _assert_secret_absent(
        write_token,
        got,
        validated,
        created,
        updated,
        cli_created.output,
        cli_updated.output,
        approved.output,
    )
    _assert_secret_absent(admin_token, approved.output)


def test_auth_off_sends_no_header_and_both_client_types_still_work(
    client_boundary,
    source_file,
    monkeypatch,
):
    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("CAO_AUTH_JWKS_URI", raising=False)
    monkeypatch.delenv("CAO_AUTH_LOCAL_TOKEN", raising=False)
    assert auth_module.is_auth_enabled() is False
    assert auth_module.get_client_bearer() is None

    mcp_results = [
        asyncio.run(_mcp_tool("workflow_get")(name="wf")),
        asyncio.run(_mcp_tool("workflow_validate")(source=SOURCE)),
        asyncio.run(_mcp_tool("workflow_create")(name="wf", source=SOURCE)),
        asyncio.run(
            _mcp_tool("workflow_update")(
                name="wf",
                source=SOURCE,
                expected_hash="sha256:test",
            )
        ),
    ]
    runner = CliRunner()
    cli_results = [
        runner.invoke(
            cli_workflow.workflow,
            ["create", "wf", "--from-file", str(source_file)],
        ),
        runner.invoke(
            cli_workflow.workflow,
            [
                "update",
                "wf",
                "--from-file",
                str(source_file),
                "--expected-hash",
                "sha256:test",
            ],
        ),
        runner.invoke(cli_workflow.workflow, ["approve", PLAN_ID]),
    ]

    assert all(result["ok"] is True for result in mcp_results)
    assert all(result.exit_code == 0 for result in cli_results)
    assert client_boundary.seen_headers == [None] * 7


def test_missing_token_401_is_actionable_without_becoming_unreachable(
    auth_enabled_env,
    jwks_boundary,
    client_boundary,
    source_file,
    monkeypatch,
):
    monkeypatch.delenv("CAO_AUTH_LOCAL_TOKEN", raising=False)

    mcp_result = asyncio.run(_mcp_tool("workflow_validate")(source=SOURCE))
    cli_result = CliRunner().invoke(
        cli_workflow.workflow,
        ["create", "wf", "--from-file", str(source_file)],
    )

    assert mcp_result["ok"] is False
    assert mcp_result["class"] != "unreachable"
    assert "CAO_AUTH_LOCAL_TOKEN" in mcp_result["error"]
    assert cli_result.exit_code == 1
    assert "CAO_AUTH_LOCAL_TOKEN" in cli_result.output
    assert "unreachable" not in cli_result.output.lower()
    assert client_boundary.seen_headers == [None, None]


def test_invalid_token_401_is_distinct_and_never_discloses_the_token(
    auth_enabled_env,
    jwks_boundary,
    client_boundary,
    source_file,
    monkeypatch,
    caplog,
):
    _split_client_from_authenticated_server(client_boundary, monkeypatch)
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", INVALID_TOKEN)
    assert auth_module.get_local_bearer() is None
    caplog.set_level(logging.DEBUG)

    mcp_result = asyncio.run(_mcp_tool("workflow_validate")(source=SOURCE))
    cli_result = CliRunner().invoke(
        cli_workflow.workflow,
        ["create", "wf", "--from-file", str(source_file)],
    )

    assert mcp_result["ok"] is False
    assert mcp_result["class"] != "unreachable"
    assert "rejected" in mcp_result["error"].lower()
    assert "requires authentication; configure" not in mcp_result["error"].lower()
    assert cli_result.exit_code == 1
    assert "rejected" in cli_result.output.lower()
    assert "requires authentication; configure" not in cli_result.output.lower()
    _assert_secret_absent(
        INVALID_TOKEN,
        mcp_result,
        cli_result.output,
        cli_result.stderr,
        cli_result.exception,
        caplog.text,
    )


def test_valid_read_token_gets_403_from_write_clients(
    auth_enabled_env,
    jwks_boundary,
    rsa_keys,
    client_boundary,
    source_file,
    monkeypatch,
):
    token = _set_token(monkeypatch, rsa_keys, "cao:read")

    mcp_result = asyncio.run(_mcp_tool("workflow_create")(name="wf", source=SOURCE))
    cli_result = CliRunner().invoke(
        cli_workflow.workflow,
        [
            "update",
            "wf",
            "--from-file",
            str(source_file),
            "--expected-hash",
            "sha256:test",
        ],
    )

    assert mcp_result["ok"] is False
    assert mcp_result["class"] == "error"
    assert "forbidden" in mcp_result["error"].lower()
    assert "cao:write" in mcp_result["error"]
    assert cli_result.exit_code == 1
    assert "forbidden" in cli_result.output.lower()
    assert "cao:write" in cli_result.output
    assert "unreachable" not in cli_result.output.lower()
    _assert_secret_absent(token, mcp_result, cli_result.output, cli_result.exception)


def test_valid_write_token_gets_403_from_admin_only_approval(
    auth_enabled_env,
    jwks_boundary,
    rsa_keys,
    client_boundary,
    monkeypatch,
):
    token = _set_token(monkeypatch, rsa_keys, "cao:write")

    result = CliRunner().invoke(cli_workflow.workflow, ["approve", PLAN_ID])

    assert result.exit_code == 1
    assert "cao:admin" in result.output
    assert "unreachable" not in result.output.lower()
    _assert_secret_absent(token, result.output, result.stderr, result.exception)
