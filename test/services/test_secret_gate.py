"""Tests for the federated-write credential gate (``scan_for_secrets``).

The gate is a pure deny-list heuristic: it returns the NAME of the first
matching credential pattern, or ``None`` when the content looks clean. It is
used ONLY on ``scope="federated"`` writes.
"""

import pytest

from cli_agent_orchestrator.services.secret_gate import scan_for_secrets

# ---------------------------------------------------------------------------
# Positive cases — each must return a non-None pattern name.
# ---------------------------------------------------------------------------

_POSITIVE = [
    ("aws_access_key", "creds: AKIAIOSFODNN7EXAMPLE in config"),
    (
        "pem_private_key",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
    ),
    ("secret_assignment", "password=hunter2longenough"),
    # Canonical HTTP header form: 'Authorization: Bearer <space> <token>'.
    # The separator after the keyword may be whitespace, ':' or '='.
    ("bearer_token", "Authorization: Bearer abcdef0123456789ABCDEF"),
    ("github_pat", "ghp_" + "a" * 36),
    ("gitlab_pat", "glpat-" + "x" * 20),
]


@pytest.mark.parametrize("label,content", _POSITIVE, ids=[p[0] for p in _POSITIVE])
def test_scan_for_secrets_positive(label, content):
    """Credential-shaped content returns a non-None pattern name."""
    result = scan_for_secrets(content)
    assert result is not None
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Negative cases — each must return None.
# ---------------------------------------------------------------------------

_NEGATIVE = [
    ("plain_prose", "This is a normal note about how pytest fixtures work."),
    ("bare_uuid", "session id 550e8400-e29b-41d4-a716-446655440000"),
    ("short_token", "token=abc"),
    ("git_sha", "fixed in commit a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"),
    (
        "normal_markdown",
        "# Title\n\n- bullet one\n- bullet two\n\nSome **bold** text and a [link](http://x).",
    ),
]


@pytest.mark.parametrize("label,content", _NEGATIVE, ids=[n[0] for n in _NEGATIVE])
def test_scan_for_secrets_negative(label, content):
    """Benign content returns None."""
    assert scan_for_secrets(content) is None


def test_scan_for_secrets_empty():
    """Empty content is clean."""
    assert scan_for_secrets("") is None


def test_bearer_space_form_is_caught():
    """The canonical space-separated Bearer header is caught by the gate."""
    assert scan_for_secrets("Authorization: Bearer abcdef0123456789ABCDEF") == "bearer_token"
