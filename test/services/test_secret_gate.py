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


# Fixtures are assembled at runtime so no credential-shaped literal sits in the
# source: the repo's gitleaks gate and GitHub push protection scan test files
# too, and these are the documented AWS example key and the jwt.io sample.
_AWS_DOC_SAMPLE = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
_JWT_SAMPLE = ".".join(
    [
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    ]
)

# ---------------------------------------------------------------------------
# Credential families added after the original six patterns. Each case names
# the pattern the gate must attribute the match to; a specific name matters
# because it is the only thing a caller may log.
# ---------------------------------------------------------------------------

_VENDOR_POSITIVE = [
    ("anthropic_api_key", "key: sk-ant-api03-" + "Qz7" * 30),
    ("openai_api_key", "OPENAI: sk-proj-" + "Ab9" * 20),
    ("openai_api_key", "sk-svcacct-" + "Ab9" * 20),
    ("openai_api_key", "sk-" + "a1B2c3D4e5F6g7H8i9J0" + "T3BlbkFJ" + "k1L2m3N4o5P6q7R8s9T0"),
    ("github_fine_grained_pat", "github_pat_" + "A" * 22 + "_" + "b" * 59),
    ("github_pat", "gho_" + "c" * 36),
    ("github_pat", "ghu_" + "d" * 36),
    ("github_pat", "ghr_" + "e" * 36),
    ("slack_token", "xox" + "b-1234567890-1234567890-AbCdEfGhIjKlMnOp"),
    ("slack_token", "xox" + "p-1234567890-1234567890-1234567890-abcdef0123456789"),
    ("slack_token", "xox" + "e-1-AbCdEfGhIjKlMnOpQrStUv"),
    (
        "jwt",
        _JWT_SAMPLE,
    ),
    ("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY=" + _AWS_DOC_SAMPLE),
    ("aws_secret_access_key", 'aws_secret_access_key: "' + _AWS_DOC_SAMPLE + '"'),
    # STS / IAM JSON responses carry the key without the word "aws" nearby.
    ("aws_secret_access_key", '{"SecretAccessKey": "' + _AWS_DOC_SAMPLE + '"}'),
]


@pytest.mark.parametrize(
    "expected,content",
    _VENDOR_POSITIVE,
    ids=[f"{p[0]}-{i}" for i, p in enumerate(_VENDOR_POSITIVE)],
)
def test_vendor_credential_families_named_specifically(expected, content):
    assert scan_for_secrets(content) == expected


_VENDOR_NEGATIVE = [
    (
        "bare_40_base64_no_aws_context",
        "digest " + _AWS_DOC_SAMPLE + " of the blob",
    ),
    ("sk_learn_prose", "we use sk-learn and sk-learn-extra for the classifier"),
    ("short_sk_prefix", "sk-proj-short"),
    ("two_part_dotted_base64", "eyJhbGciOiJIUzI1NiJ9.notajwtpayloadsegment"),
    ("xoxo_prose", "signed xoxo-love-and-hugs-1234567890"),
    ("github_pat_wrong_segment_lengths", "github_pat_" + "A" * 10 + "_" + "b" * 20),
    ("ghx_unknown_github_prefix", "ghx_" + "a" * 36),
    ("aws_access_key_id_context_only", "aws_access_key_id = AKIA-not-a-key-here"),
    (
        "aws_prose_then_40_digits",
        "AWS access logs are stored at: " + "1234567890" * 4,
    ),
    (
        "aws_prose_then_40_hex_sha",
        "aws access review, see commit " + "a1b2c3d4e5f6a7b8c9d0" + "e1f2a3b4c5d6e7f8a9b0",
    ),
    ("slack_docs_path", "https://api.slack.com/xoxb-example-token"),
]


@pytest.mark.parametrize("label,content", _VENDOR_NEGATIVE, ids=[n[0] for n in _VENDOR_NEGATIVE])
def test_vendor_lookalikes_stay_clean(label, content):
    assert scan_for_secrets(content) is None


class TestZeroWidthEvasion:
    """A zero-width code point inside a prefix must not hide a credential."""

    @pytest.mark.parametrize(
        "zw", ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"], ids=lambda c: f"U+{ord(c):04X}"
    )
    def test_split_aws_prefix_is_still_caught(self, zw):
        assert scan_for_secrets(f"AK{zw}IAIOSFODNN7EXAMPLE") == "aws_access_key"

    def test_split_vendor_prefix_is_still_caught(self):
        assert scan_for_secrets("sk-\u200bant-" + "x" * 30) == "anthropic_api_key"
