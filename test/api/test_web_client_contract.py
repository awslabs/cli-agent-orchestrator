"""The shipped Web UI's request shapes are accepted by the routes they target.

Round 4 of PR #948: the in-session create started requiring ``caller_id`` and the
Web UI's "Add Agent" button kept posting without one. Both sides had green tests,
because the client test asserted the URL the client builds and the server test
asserted the route refuses that URL: client/server drift the suite could not see.

This module closes that class structurally. It reads the request each client
function builds FROM ``web/src/api.ts`` (the template literal handed to
``fetchJSON``), renders it with sample values the same way the browser would,
and sends it through the real FastAPI app with only the service layer mocked.
A route tightening that breaks a first-party UI action now fails here, in the
Python suite, and a client change that points at a route the server does not
serve fails here too. The parser is deliberately strict: when it cannot find or
read a client function the test fails rather than passing vacuously.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Dict, Tuple
from unittest.mock import AsyncMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import Terminal

API_TS = Path(__file__).resolve().parents[2] / "web" / "src" / "api.ts"
_TS = "cli_agent_orchestrator.services.terminal_service"


def _template_literal(src: str, start: int) -> Tuple[str, int]:
    """Return the body of the template literal whose opening backtick is at ``start``.

    Tracks ``${ ... }`` nesting and nested template literals, so an optional
    query fragment written as ``${x ? `&k=${x}` : ''}`` is kept whole.
    """
    assert src[start] == "`", src[start : start + 20]
    i, depth, out = start + 1, 0, []
    while i < len(src):
        ch = src[i]
        if depth == 0 and ch == "`":
            return "".join(out), i
        if src.startswith("${", i):
            depth += 1
            out.append("${")
            i += 2
            continue
        if ch == "`" and depth > 0:
            inner, end = _template_literal(src, i)
            out.append("`" + inner + "`")
            i = end + 1
            continue
        if ch == "}" and depth > 0:
            depth -= 1
        out.append(ch)
        i += 1
    raise AssertionError("unterminated template literal")


def client_request(name: str) -> Tuple[str, str]:
    """``(method, url_template)`` for the ``api.<name>`` client function.

    The create calls are ``async`` since they await ``createBudgetMs()`` for
    their deadline (#1047), so the arrow may carry that keyword.
    """
    src = API_TS.read_text(encoding="utf-8")
    head = re.search(
        rf"^\s*{re.escape(name)}:\s*(?:async\s+)?\(.*?\)\s*=>\s*fetchJSON<[^>]*>\(\s*",
        src,
        re.M | re.S,
    )
    assert head, f"api.{name} not found in {API_TS.name}; update this parser with the client"
    template, end = _template_literal(src, head.end())
    tail = src[end : src.find("\n", end)]
    method = re.search(r"method:\s*'([A-Z]+)'", tail)
    return (method.group(1) if method else "GET"), template


def _split_ternary(expr: str) -> Tuple[str, str, str]:
    q = expr.index("?")
    cond, rest = expr[:q], expr[q + 1 :]
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "`":
            depth ^= 1
        elif ch == ":" and depth == 0:
            return cond.strip(), rest[:i].strip(), rest[i + 1 :].strip()
    raise AssertionError(f"ternary without ':' in {expr!r}")


def render(template: str, values: Dict[str, object]) -> str:
    """Render ``template`` as the browser would for ``values`` (missing = undefined)."""
    out, i = [], 0
    while i < len(template):
        if template.startswith("${", i):
            depth, j = 1, i + 2
            while depth:
                if template[j] == "`":
                    _, j = _template_literal(template, j)
                elif template.startswith("${", j):
                    depth += 1
                    j += 1
                elif template[j] == "}":
                    depth -= 1
                j += 1
            out.append(_evaluate(template[i + 2 : j - 1].strip(), values))
            i = j
        else:
            out.append(template[i])
            i += 1
    return "".join(out)


def _evaluate(expr: str, values: Dict[str, object]) -> str:
    if "?" in expr:
        cond, yes, no = _split_ternary(expr)
        branch = yes if values.get(cond) else no
        if branch.startswith("`"):
            return render(branch[1:-1], values)
        return branch.strip("'\"")
    call = re.fullmatch(r"encodeURIComponent\((\w+)\)", expr)
    name = call.group(1) if call else expr
    assert re.fullmatch(r"\w+", name), f"unsupported client expression {expr!r}"
    assert name in values, f"no sample value for client argument {name!r}"
    value = str(values[name])
    return urllib.parse.quote(value, safe="") if call else value


def _created() -> Terminal:
    return Terminal(id="c0ffee01", name="w", provider="claude_code", session_name="cao-sess")


class TestAddAgentButton:
    """``AgentPanel.handleAddAgent`` -> ``api.addTerminalToSession``."""

    def test_the_client_targets_the_operator_form(self):
        method, template = client_request("addTerminalToSession")
        assert method == "POST"
        assert template.startswith("/sessions/${sessionName}/terminals:operator?")
        assert "caller_id" not in template and "caller_ceiling" not in template

    @pytest.mark.parametrize("working_directory", [None, "/home/user/project"])
    def test_the_shipped_request_is_accepted_by_the_real_route(self, client, working_directory):
        method, template = client_request("addTerminalToSession")
        url = render(
            template,
            {
                "sessionName": "cao-sess",
                "provider": "claude_code",
                "agentProfile": "developer",
                **({"workingDirectory": working_directory} if working_directory else {}),
            },
        )
        with patch(f"{_TS}.create_terminal", new=AsyncMock(return_value=_created())) as create:
            response = client.request(method, url)
        assert response.status_code == 201, (url, response.text)
        kwargs = create.call_args.kwargs
        assert kwargs["session_name"] == "cao-sess"
        assert kwargs["agent_profile"] == "developer"
        assert kwargs["provider"] == "claude_code"
        assert kwargs["working_directory"] == working_directory
        # The UI is the operator: it names no caller and the server records the
        # profile's declared ceiling, exactly as POST /sessions does.
        assert not {"caller_id", "delegating_terminal_id", "caller_ceiling"} & set(kwargs)


class TestNewSessionForm:
    """``api.createSession`` -> ``POST /sessions`` (the operator launch)."""

    def test_the_shipped_request_is_accepted_by_the_real_route(self, client):
        method, template = client_request("createSession")
        url = render(template, {"provider": "claude_code", "agentProfile": "developer"})
        assert method == "POST"
        with patch(
            "cli_agent_orchestrator.api.main.session_service.create_session",
            new=AsyncMock(return_value=_created()),
        ) as create:
            response = client.request(method, url)
        assert response.status_code == 201, (url, response.text)
        assert create.call_args.kwargs["agent_profile"] == "developer"


def test_the_parser_fails_loudly_on_an_unknown_client_function():
    with pytest.raises(AssertionError, match="not found"):
        client_request("noSuchClientFunction")
