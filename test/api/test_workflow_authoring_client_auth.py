"""Credential forwarding across PR699 workflow authoring and execution clients."""

from __future__ import annotations

import ast
import asyncio
import inspect
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

from cli_agent_orchestrator.api import main as api_main
from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.cli.commands import workflow as cli_workflow
from cli_agent_orchestrator.mcp_server import server as mcp_server
from cli_agent_orchestrator.models.workflow import ScriptSpec, ScriptValidationResult
from cli_agent_orchestrator.security import auth as auth_module
from cli_agent_orchestrator.services import (
    approval_store,
    script_lint,
    script_runner,
    workflow_spec_service,
)

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

    def delete(self, url: str, **kwargs: Any):
        headers = kwargs.get("headers")
        self.seen_headers.append(headers)
        with self._server_environment():
            return self.client.delete(self._path(url), headers=headers)


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
    monkeypatch.setattr(workflow_spec_service, "_safe_spec_path", lambda path: path)
    monkeypatch.setattr(workflow_spec_service, "delete_workflow", lambda name: None)
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
    monkeypatch.setattr(cli_workflow.requests, "get", adapter.get)
    monkeypatch.setattr(cli_workflow.requests, "post", adapter.post)
    monkeypatch.setattr(cli_workflow.requests, "put", adapter.put)
    monkeypatch.setattr(cli_workflow.requests, "delete", adapter.delete)
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


def test_valid_tokens_cross_all_authoring_client_boundaries(
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
    assert auth_module.get_local_bearer() == read_token
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
    cli_got = runner.invoke(cli_workflow.workflow, ["get", "wf"])
    cli_validated = runner.invoke(
        cli_workflow.workflow,
        ["validate", str(source_file)],
    )
    assert created["ok"] is True
    assert updated["ok"] is True
    assert cli_created.exit_code == 0, cli_created.output
    assert cli_updated.exit_code == 0, cli_updated.output
    assert cli_got.exit_code == 0, cli_got.output
    assert cli_validated.exit_code == 0, cli_validated.output

    admin_token = _set_token(monkeypatch, rsa_keys, "cao:admin")
    approved = runner.invoke(cli_workflow.workflow, ["approve", PLAN_ID])
    deleted = runner.invoke(cli_workflow.workflow, ["delete", "wf", "--yes"])
    assert approved.exit_code == 0, approved.output
    assert deleted.exit_code == 0, deleted.output

    expected = [
        f"Bearer {read_token}",
        f"Bearer {read_token}",
        f"Bearer {write_token}",
        f"Bearer {write_token}",
        f"Bearer {write_token}",
        f"Bearer {write_token}",
        f"Bearer {write_token}",
        f"Bearer {write_token}",
        f"Bearer {admin_token}",
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
        cli_got.output,
        cli_validated.output,
        approved.output,
        deleted.output,
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
    _assert_secret_absent(admin_token, approved.output, deleted.output)


def test_local_token_crosses_cli_and_mcp_run_and_start_boundaries(monkeypatch):
    token = "run-client-local-token"
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", token)
    calls: list[tuple[str, dict[str, str] | None, float]] = []

    def _post(url: str, **kwargs: Any):
        calls.append((url, kwargs.get("headers"), kwargs["timeout"]))
        if url.endswith("/workflows/runs:submit"):
            return SimpleNamespace(
                status_code=202,
                json=lambda: {
                    "run_id": "run-1",
                    "state": "running",
                    "links": {"status": "/workflows/runs/run-1"},
                },
            )
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"run_id": "run-1", "state": "completed", "steps": []},
        )

    monkeypatch.setattr(mcp_server.requests, "post", _post)

    def _get(url: str, **kwargs: Any):
        calls.append((url, kwargs.get("headers"), kwargs["timeout"]))
        if url.endswith("/plan"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "run_id": "run-1",
                    "plan_id": PLAN_ID,
                    "approved": True,
                },
            )
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"run_id": "run-1", "state": "completed", "steps": []},
        )

    monkeypatch.setattr(cli_workflow.requests, "get", _get)
    monkeypatch.setattr(mcp_server.requests, "get", _get)

    mcp_run = asyncio.run(_mcp_tool("workflow_run")(name_or_path="wf"))
    mcp_start = asyncio.run(_mcp_tool("workflow_start")(name_or_path="wf"))
    mcp_plan = asyncio.run(_mcp_tool("workflow_plan_approval")(run_id="run-1"))
    runner = CliRunner()
    cli_run = runner.invoke(cli_workflow.workflow, ["run", "wf", "--wait"])
    cli_start = runner.invoke(cli_workflow.workflow, ["run", "wf", "--detach"])
    cli_follow = runner.invoke(cli_workflow.workflow, ["run", "wf"])
    cli_follow_json = runner.invoke(cli_workflow.workflow, ["run", "wf", "--json"])

    assert mcp_run["ok"] is True
    assert mcp_start["ok"] is True
    assert mcp_plan["ok"] is True
    assert cli_run.exit_code == 0, cli_run.output
    assert cli_start.exit_code == 0, cli_start.output
    assert cli_follow.exit_code == 0, cli_follow.output
    assert cli_follow_json.exit_code == 0, cli_follow_json.output
    assert [headers for _, headers, _ in calls] == [{"Authorization": f"Bearer {token}"}] * 9
    assert calls[0][2] == cli_workflow.WORKFLOW_RUN_REQUEST_TIMEOUT
    assert calls[3][2] == cli_workflow.WORKFLOW_RUN_REQUEST_TIMEOUT


def _registered_workflow_mcp_names() -> set[str]:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    return {tool.name for tool in tools if tool.name.startswith("workflow_")}


def _contains_auth_derivation(node: ast.AST, derived_names: set[str]) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in derived_names:
            return True
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name) and func.id == "_auth_headers":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "_auth_headers":
            return True
    return False


def _request_auth_failures(
    module: object, roots: set[str] | None = None
) -> tuple[list[str], list[str]]:
    """Audit direct requests hops in reachable functions.

    This fails closed for direct ``requests`` calls in the scanned module,
    including imported aliases and module-level helpers passed by name. HTTP
    helpers from other modules (for example ``mcp_utils.get_json``) remain
    outside this AST scan and are pinned by the executed client tests instead.
    Parameters named ``auth_headers`` are trusted without inspecting their call
    sites, and module-level ``requests.Session`` instances and their method calls
    are not scanned. The taint analysis is flow-insensitive, so reassigned or
    conditional header values are not distinguished, and function-local
    ``import requests as X`` aliases are not resolved.
    """
    tree = ast.parse(inspect.getsource(module))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    request_modules = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "requests"
    }
    request_functions = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "requests"
        for alias in node.names
    }
    reachable = set(functions) if roots is None else set(roots)
    missing_roots = reachable - functions.keys()
    assert not missing_roots, (
        "registered workflow tools have no matching top-level definition: "
        f"{sorted(missing_roots)}"
    )
    pending = list(reachable)
    while pending:
        node = functions[pending.pop()]
        referenced_functions = {
            child.id
            for child in ast.walk(node)
            if isinstance(child, ast.Name) and child.id in functions
        }
        for function_name in referenced_functions - reachable:
            reachable.add(function_name)
            pending.append(function_name)

    failures: list[str] = []
    inspected_calls: list[str] = []
    for name in sorted(reachable):
        node = functions[name]
        derived_names = {
            arg.arg for arg in (*node.args.args, *node.args.kwonlyargs) if arg.arg == "auth_headers"
        }
        for child in ast.walk(node):
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                value = child.value
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                if value is not None and _contains_auth_derivation(value, derived_names):
                    derived_names.update(
                        target.id for target in targets if isinstance(target, ast.Name)
                    )
        for call in (child for child in ast.walk(node) if isinstance(child, ast.Call)):
            func = call.func
            is_module_call = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in request_modules
            )
            is_imported_call = isinstance(func, ast.Name) and func.id in request_functions
            if not (is_module_call or is_imported_call):
                continue
            inspected_calls.append(f"{name}:{call.lineno}")
            headers = next((kw.value for kw in call.keywords if kw.arg == "headers"), None)
            if headers is None or not _contains_auth_derivation(headers, derived_names):
                failures.append(f"{name}:{call.lineno}")
    return failures, inspected_calls


def test_every_registered_workflow_mcp_http_hop_forwards_auth():
    """Registered tools authenticate every direct requests hop in this module."""
    failures, inspected = _request_auth_failures(mcp_server, _registered_workflow_mcp_names())
    assert inspected
    assert len(inspected) >= 17
    assert failures == []


def test_every_workflow_cli_http_hop_forwards_auth():
    """CLI workflow functions authenticate every direct requests hop in this module."""
    failures, inspected = _request_auth_failures(cli_workflow)
    assert inspected
    assert len(inspected) >= 21
    assert failures == []


@pytest.mark.parametrize(
    "args",
    [
        ["list"],
        ["status", "run-1"],
        ["status"],
        ["runs"],
        ["wait", "run-1"],
        ["result", "run-1"],
        ["resume", "run-1"],
        ["step", "run-1", "step-1"],
        ["cancel", "run-1"],
        ["events", "run-1", "--no-follow"],
    ],
)
def test_remaining_workflow_cli_verbs_forward_auth(monkeypatch, args):
    token = "remaining-cli-token"
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", token)
    seen: list[dict[str, str] | None] = []

    def _response(url: str, **kwargs: Any):
        seen.append(kwargs.get("headers"))
        if url.endswith("/workflows"):
            body = []
        elif url.endswith("/workflows/runs"):
            body = [{"run_id": "run-1", "state": "completed"}]
        elif url.endswith("/result") or url.endswith("/resume"):
            body = {"run_id": "run-1", "state": "completed", "steps": []}
        elif "/steps/" in url:
            body = {"run_id": "run-1", "step_id": "step-1", "error": None}
        elif url.endswith("/events"):
            body = []
        else:
            body = {"run_id": "run-1", "state": "completed"}
        return SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(cli_workflow.requests, "get", _response)
    monkeypatch.setattr(cli_workflow.requests, "post", _response)

    result = CliRunner().invoke(cli_workflow.workflow, args)

    assert result.exit_code == 0, result.output
    assert seen
    assert seen == [{"Authorization": f"Bearer {token}"}] * len(seen)


def test_mcp_run_authenticates_terminal_root_lookup_and_first_post(
    auth_enabled_env,
    jwks_boundary,
    rsa_keys,
    client_boundary,
    monkeypatch,
    tmp_path,
):
    _split_client_from_authenticated_server(client_boundary, monkeypatch)
    token = _set_token(monkeypatch, rsa_keys, "cao:write")
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setattr(
        api_main.terminal_service, "get_working_directory", lambda tid: str(tmp_path)
    )

    async def _run(spec, inputs, run_id, *, working_directory=None):
        assert working_directory == str(tmp_path)
        return SimpleNamespace(
            model_dump=lambda: {
                "run_id": run_id,
                "state": "completed",
                "steps": [],
            }
        )

    monkeypatch.setattr(script_runner, "run_script_workflow", _run)

    result = asyncio.run(_mcp_tool("workflow_run")(name_or_path="wf"))

    assert result["ok"] is True
    assert [headers["Authorization"] for headers in client_boundary.seen_headers] == [
        f"Bearer {token}",
        f"Bearer {token}",
    ]


def test_auth_off_sends_no_header_and_both_client_types_still_work(
    client_boundary,
    source_file,
    monkeypatch,
):
    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("CAO_AUTH_JWKS_URI", raising=False)
    monkeypatch.delenv("CAO_AUTH_LOCAL_TOKEN", raising=False)
    assert auth_module.is_auth_enabled() is False
    assert auth_module.get_local_bearer() is None

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
    assert auth_module.get_local_bearer() == INVALID_TOKEN
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
