"""Every hand-rolled call to cao-server must carry the operator's credential (#745).

``api_request`` attaches ``Authorization: Bearer`` for the callers that go through
it, but a large number of call sites assemble their own ``requests.get`` /
``requests.post`` against ``API_BASE_URL``. Each one that forgot ``headers=``
worked perfectly against the default-off local server and returned 401 the moment
an operator enabled auth — the deployment least likely to be the one anybody tested
(Copilot review on #802).

Auditing that by eye is exactly the kind of thing that decays: the omission is
invisible at the call site and silent in every local test. So this test walks the
source instead of exercising the calls, which is the only way to cover a site nobody
has written a test for yet, including the next one somebody adds.

The rule is deliberately about the PRESENCE of a headers argument, not its value:
which helper supplies it differs by module (``auth_headers`` in the CLI,
``_auth_headers`` in the MCP server and orchestration), and some sites merge in
worker-gateway headers as well. A site that passes ``headers=`` is one a reviewer
can read; a site that passes none cannot be right.
"""

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator"

REQUEST_VERBS = {"get", "post", "put", "delete", "patch", "request", "head", "options"}

# The one call that must NOT carry CAO's credential: a profile download from an
# allowlisted PUBLIC host. Sending the operator's bearer token to a third party
# would leak it, so this site is exempt by intent rather than by omission.
EXEMPT = {
    ("services/install_service.py", "safe_url"),
}


def _bare_request_calls():
    """Every ``requests.<verb>(...)`` in the package, with its headers status."""
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in REQUEST_VERBS:
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != "requests":
                continue
            url = ast.unparse(node.args[0]) if node.args else "<no url argument>"
            yield (
                str(path.relative_to(SRC_ROOT)),
                node.lineno,
                url,
                "headers" in {kw.arg for kw in node.keywords},
            )


def test_there_are_call_sites_to_check():
    """Guard the guard: an AST walk that matches nothing passes vacuously."""
    assert len(list(_bare_request_calls())) > 50


def test_every_direct_request_passes_headers():
    missing = [
        f"{rel}:{lineno} requests -> {url}"
        for rel, lineno, url, has_headers in _bare_request_calls()
        if not has_headers and (rel, url) not in EXEMPT
    ]
    assert not missing, (
        "these calls reach cao-server without an Authorization header, so they "
        "return 401 on an auth-enabled deployment:\n  " + "\n  ".join(missing)
    )


@pytest.mark.parametrize("rel,url", sorted(EXEMPT))
def test_the_exemption_still_describes_a_real_unauthenticated_call(rel, url):
    """An exemption that no longer matches anything is a stale licence.

    If the install download grows a headers argument, or moves, this fails and the
    list above has to be revisited rather than quietly covering nothing.
    """
    matches = [
        (lineno, has_headers)
        for site_rel, lineno, site_url, has_headers in _bare_request_calls()
        if (site_rel, site_url) == (rel, url)
    ]
    assert matches, f"exempted call {rel} -> {url} no longer exists"
    assert all(not has_headers for _, has_headers in matches)
