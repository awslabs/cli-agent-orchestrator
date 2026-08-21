# ABOUTME: Regression tests for the custom rules + allowlist in .gitleaks.toml.
# ABOUTME: Skips when the gitleaks binary is absent (e.g. the pip-only CI test job).
"""Lock in the behaviour of the hand-written gitleaks rules (issue #457).

The two custom rules in ``.gitleaks.toml`` (AWS secret access keys; GitHub
fine-grained PATs) and the AWS-example allowlist were tuned by hand against
gitleaks 8.30.1 during review — closing real gaps in the default ruleset while
avoiding false positives on placeholders, hashes, and the repo's live-TUI
fixtures. These tests pin that boundary so a later config edit can't silently
regress it.

gitleaks is a Go binary, not a Python dependency, so it is absent from the
pip-only CI unit-test job (these tests skip there). They are executed by the
dedicated ``config-tests`` job in .github/workflows/secret-scan.yml, which
installs gitleaks on PATH and runs this file explicitly, and by any local run
where the binary is installed.
"""

import random
import shutil
import string
import subprocess
from pathlib import Path

import pytest


def _rand(n: int) -> str:
    """A random high-entropy [A-Za-z0-9] run of length n.

    Real tokens are random; hand-crafted samples with sequential runs
    (``1234567890``, ``AbCdEf``) can trip gitleaks' built-in ``stopwords`` /
    low-signal heuristics and read as non-secrets. Generating random material
    keeps these tests exercising the rule's shape/boundary, not those filters.
    Uses the stdlib ``random`` (test data only — not a security context).
    """
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _REPO_ROOT / ".gitleaks.toml"

pytestmark = pytest.mark.skipif(
    shutil.which("gitleaks") is None,
    reason="gitleaks binary not on PATH (Go tool; absent in the pip-only test job)",
)


def _scan(content: str) -> int:
    """Scan ``content`` with the repo config via stdin, return the exit code.

    gitleaks exits 1 when a secret is detected, 0 when clean (``--exit-code 1``).
    ``--pipe`` reads the input from stdin, so the (synthetic) test material is
    never written to disk — this keeps the test itself from being a clear-text
    secret sink, which is exactly the kind of thing this PR guards against.
    """
    result = subprocess.run(
        [
            "gitleaks",
            "detect",
            "--pipe",
            "--config",
            str(_CONFIG),
            "--redact",
            "--exit-code",
            "1",
        ],
        input=content.encode(),
        capture_output=True,
        timeout=30,
    )
    return result.returncode


def _caught(content: str) -> bool:
    return _scan(content) == 1


# --- custom rules must catch real credential shapes the defaults miss ---


def test_aws_secret_access_key_caught():
    # 40-char secret in an aws-labelled assignment (default rules miss this).
    assert _caught(f'aws_secret_access_key = "{_rand(40)}"')


def test_aws_secret_caught_with_base64_chars():
    # Real AWS secrets use the full base64 alphabet (A-Z, a-z, 0-9, +, /).
    # Ensure +/ in the 40-char body don't break detection.
    body = _rand(30) + "+/+" + _rand(7)  # 40 chars total with +/
    assert _caught(f'aws_secret_access_key = "{body}"')


def test_aws_secret_caught_when_terminated_by_punctuation_or_eof():
    # The end-boundary must accept ; , ) & etc. AND end-of-file (no trailing
    # newline), not only quote/whitespace.
    secret = _rand(40)
    for term in (";", ",", ")", "&", ""):  # "" = key sits at EOF
        content = f"aws_secret_access_key={secret}{term}"
        label = repr(term) if term else "EOF"
        assert _caught(content), f"missed AWS secret terminated by {label}"


def test_aws_secret_rule_requires_exactly_40_chars():
    # The custom aws-secret rule is length-anchored to 40 (real AWS secret
    # length). Verify the RULE's boundary in isolation so a config edit that
    # loosens {40} is caught. (Note: the full repo config also runs gitleaks'
    # default generic-api-key rule, which independently flags long high-entropy
    # key values — so a 41-char blob is still caught overall; here we assert the
    # custom rule specifically, not the whole config.)
    import re

    rule = None
    try:
        import tomllib
    except ModuleNotFoundError:  # py3.10
        import tomli as tomllib
    cfg = tomllib.loads(_CONFIG.read_text())
    for r in cfg.get("rules", []):
        if r["id"] == "aws-secret-access-key":
            rule = re.compile(r["regex"])
    assert rule is not None, "aws-secret-access-key rule not found"

    assert rule.search('aws_secret_access_key = "' + _rand(40) + '"')  # 40 → match
    assert not rule.search("aws_secret_access_key = " + _rand(39) + " ")  # 39 → no match
    # 41 → no match: the delimiter-anchored prefix can't line up a 40-char window
    # inside a 41-char run. Asserted at the regex level to avoid interference from
    # the default generic-api-key rule (which flags long high-entropy values).
    assert not rule.search('aws_secret_access_key = "' + _rand(41) + '"')


def test_github_fine_grained_pat_caught():
    # github_pat_ + 22 chars + _ + 59 chars, random/high-entropy (real shape).
    tok = f"github_pat_{_rand(22)}_{_rand(59)}"
    assert _caught(f'token = "{tok}"')


# --- must NOT fire on placeholders / non-secrets ---


def test_placeholders_not_flagged():
    assert not _caught(
        'API_KEY = "your-api-key-here"\n'
        'password = "changeme"\n'
        'token = os.environ["CAO_TOKEN"]\n'
    )


def test_hashes_and_ids_not_flagged():
    assert not _caught(
        'git_sha = "9f2554cbe13877a43ebd9fb12490ba7aa892d438"\n'
        'uuid = "550e8400-e29b-41d4-a716-446655440000"\n'
        'sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"\n'
    )


def test_github_pat_low_entropy_placeholder_not_flagged():
    # A token of the RIGHT shape (22 + "_" + 59) but low entropy — a repeated
    # placeholder — must be rejected by the entropy=3.0 threshold, not caught.
    # (Shape alone matching would false-positive on docs; this exercises entropy.)
    seg1 = "x" * 22
    seg2 = "x" * 59
    assert not _caught(f'doc = "github_pat_{seg1}_{seg2}"')


# --- allowlist is precise, not global ---


def test_aws_example_key_allowlisted():
    assert not _caught('aws_key = "AKIAIOSFODNN7EXAMPLE"')


def test_allowlist_does_not_exempt_other_secrets():
    # The AWS-example allowlist must be scoped to that exact string only — it
    # must not blanket-exempt other credentials appearing on the same line or
    # file. A real AWS secret alongside the example key is still caught.
    secret = _rand(40)
    assert _caught(
        'aws_key = "AKIAIOSFODNN7EXAMPLE"  # allowlisted example\n'
        f'aws_secret_access_key = "{secret}"  # real secret — must fire\n'
    )
