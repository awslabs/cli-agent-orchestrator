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
_AWS_DOC_SAMPLE = "wJalrXUtn" + "FEMI/K7MD" + "ENG/bPxRf" + "iCYEXAMPL" + "EKEY"
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
    # app-level token: the documented xapp- shape, which no xox? form covers
    ("slack_token", "xa" + "pp-1-A0ABCDEFG-1234567890123-abcdef0123456789abcdef"),
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
    ("slack_app_docs", "an app-level token starts with the letters x, a, p, p and a dash"),
]


@pytest.mark.parametrize("label,content", _VENDOR_NEGATIVE, ids=[n[0] for n in _VENDOR_NEGATIVE])
def test_vendor_lookalikes_stay_clean(label, content):
    assert scan_for_secrets(content) is None


class TestZeroWidthEvasion:
    """An invisible code point inside a prefix must not hide a credential.

    Not only the five zero-width characters: any Unicode format character
    (bidi marks, soft hyphen, invisible operators, tags) and the variation
    selectors split a prefix the same way, and an attacker picks the next one.
    """

    @pytest.mark.parametrize(
        "zw",
        [
            "\u200b",  # zero width space
            "\u200c",  # zero width non-joiner
            "\u200d",  # zero width joiner
            "\u2060",  # word joiner
            "\ufeff",  # BOM
            "\u200e",  # left-to-right mark
            "\u200f",  # right-to-left mark
            "\u202e",  # right-to-left override
            "\u2066",  # left-to-right isolate
            "\u00ad",  # soft hyphen
            "\u2061",  # function application
            "\u2064",  # invisible plus
            "\u034f",  # combining grapheme joiner
            "\ufe0f",  # variation selector 16
            "\U000e0100",  # variation selector 17
            "\U000e0020",  # tag space
        ],
        ids=lambda c: f"U+{ord(c):04X}",
    )
    def test_split_aws_prefix_is_still_caught(self, zw):
        assert scan_for_secrets(f"AK{zw}IAIOSFODNN7EXAMPLE") == "aws_access_key"

    def test_class_matches_the_live_format_category_everywhere(self):
        """The class is built from planes 0-1 plus a listed plane-14 block; a
        Unicode update that puts a format character anywhere else must fail here,
        not silently pass a hidden credential."""
        import unicodedata

        from cli_agent_orchestrator.services.secret_gate import _INVISIBLE

        missing = [
            hex(cp)
            for cp in range(0x110000)
            if unicodedata.category(chr(cp)) == "Cf" and not _INVISIBLE.match(chr(cp))
        ]
        assert missing == []

    @pytest.mark.parametrize(
        "zw",
        [
            "\u0890",  # Arabic pound mark above, Unicode 14.0
            "\U000110bd",  # Kaithi number sign
            "\U00013430",  # Egyptian hieroglyph vertical joiner
            "\U0001343f",  # Egyptian hieroglyph end walled enclosure, Unicode 15.0
        ],
        ids=lambda c: f"U+{ord(c):04X}",
    )
    def test_format_controls_newer_than_the_interpreter_are_stripped_too(self, zw):
        """Python 3.10 ships Unicode 13 and 3.11 ships 14, where two of these are
        unassigned; the frozen table must catch them on every supported Python."""
        assert scan_for_secrets(f"AK{zw}IAIOSFODNN7EXAMPLE") == "aws_access_key"

    def test_frozen_table_is_the_format_category_where_the_interpreter_knows_it(self):
        """Guards the table against typos: on an interpreter whose tables are at
        least Unicode 15.0, every frozen code point must really be Cf, and the
        frozen table must be the whole category (no Cf outside it)."""
        import unicodedata

        from cli_agent_orchestrator.services.secret_gate import _FORMAT_CONTROL_RANGES

        if tuple(int(x) for x in unicodedata.unidata_version.split(".")) < (15, 0, 0):
            pytest.skip(f"unicodedata is Unicode {unicodedata.unidata_version}")
        frozen = {cp for lo, hi in _FORMAT_CONTROL_RANGES for cp in range(lo, hi + 1)}
        live = {cp for cp in range(0x110000) if unicodedata.category(chr(cp)) == "Cf"}
        assert frozen == live

    def test_visible_combining_marks_are_not_stripped(self):
        # An acute accent (Mn) is visible and belongs to its base letter; a key
        # "split" by one is not hidden, it is a different string, and stripping
        # it would corrupt legitimate text on the redact side.
        from cli_agent_orchestrator.services.secret_gate import _INVISIBLE, redact_secrets

        assert _INVISIBLE.match("\u0301") is None
        text = "cafe\u0301 and AK\u200bIAIOSFODNN7EXAMPLE"
        redacted, _ = redact_secrets(text)
        assert redacted == "cafe\u0301 and [REDACTED:aws_access_key]"

    def test_split_vendor_prefix_is_still_caught(self):
        assert scan_for_secrets("sk-\u200bant-" + "x" * 30) == "anthropic_api_key"


# A 40-character mixed-case run of the AWS secret alphabet, assembled at runtime
# so no credential-shaped literal sits in the file.
_AWS_40_CHAR_SAMPLE = "wJalrXUtn" + "FEMI/K7MD" + "ENG/bPxRf" + "iCYEXAMPL" + "EKEY"
_LONG_VALUE = "hunter2long" + "enough12345"
_HEX_VALUE = "abcdef01234" + "56789ABCDEF"


class TestScanJsonForSecrets:
    """The parsed-tree scan keeps the key context a serialised scan loses."""

    def test_secret_access_key_under_its_own_key(self):
        from cli_agent_orchestrator.services.secret_gate import scan_json_for_secrets

        doc = {
            "Credentials": {
                "AccessKeyId": "ASIA" + "X" * 16,
                "SecretAccessKey": _AWS_40_CHAR_SAMPLE,
            }
        }
        assert scan_json_for_secrets({"c": {"SecretAccessKey": _AWS_40_CHAR_SAMPLE}}) == (
            "aws_secret_access_key"
        )
        assert scan_json_for_secrets(doc) in {"aws_access_key", "aws_secret_access_key"}

    @pytest.mark.parametrize(
        "key", ["aws_secret_access_key", "AWS_SECRET_ACCESS_KEY", "secretAccessKey"]
    )
    def test_every_spelling_of_the_key_counts(self, key):
        from cli_agent_orchestrator.services.secret_gate import scan_json_for_secrets

        assert scan_json_for_secrets({key: _AWS_40_CHAR_SAMPLE}) == "aws_secret_access_key"

    def test_environment_entry_name_value_pair(self):
        from cli_agent_orchestrator.services.secret_gate import scan_json_for_secrets

        env = [
            {"name": "AWS_REGION", "value": "us-east-1"},
            {"name": "AWS_SECRET_ACCESS_KEY", "value": _AWS_40_CHAR_SAMPLE},
        ]
        assert scan_json_for_secrets({"environment": env}) == "aws_secret_access_key"

    @pytest.mark.parametrize(
        "key, value, name",
        [
            ("password", "correct horse battery staple", "secret_assignment"),
            ("api_key", _LONG_VALUE, "bearer_token"),
            ("TOKEN", _HEX_VALUE, "bearer_token"),
        ],
    )
    def test_generic_credential_keys_give_their_value_context(self, key, value, name):
        from cli_agent_orchestrator.services.secret_gate import scan_json_for_secrets

        assert scan_json_for_secrets({key: value}) == name
        assert scan_json_for_secrets([{"name": key, "value": value}]) == name

    def test_context_free_forty_character_runs_stay_clean(self):
        from cli_agent_orchestrator.services.secret_gate import scan_json_for_secrets

        # A git SHA and a mixed-case run under an unrelated key: the text
        # pattern's refusal to match without context is preserved.
        assert scan_json_for_secrets({"commit": "a" * 40, "build_id": _AWS_40_CHAR_SAMPLE}) is None
        assert scan_json_for_secrets([{"name": "BUILD_ID", "value": _AWS_40_CHAR_SAMPLE}]) is None

    def test_invisible_characters_inside_values_are_seen_unescaped(self):
        from cli_agent_orchestrator.services.secret_gate import scan_json_for_secrets

        # Through json.dumps (ensure_ascii) the U+200B would be six ASCII bytes.
        assert scan_json_for_secrets({"k": "AK\u200bIAIOSFODNN7EXAMPLE"}) == "aws_access_key"

    def test_non_string_scalars_and_empty_containers(self):
        from cli_agent_orchestrator.services.secret_gate import scan_json_for_secrets

        assert scan_json_for_secrets({"n": 1, "b": True, "x": None, "l": [], "d": {}}) is None
