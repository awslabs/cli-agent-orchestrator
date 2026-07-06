"""Tests for ``secret_gate.redact_secrets`` (#345 D5, export --redact path).

Every match of every pattern is replaced with ``[REDACTED:<name>]``; the
returned name list is ordered (``_SECRET_PATTERNS`` order) and deduped.
``scan_for_secrets`` stays untouched — a smoke check locks that.
"""

from cli_agent_orchestrator.services.secret_gate import redact_secrets, scan_for_secrets

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
AWS_KEY_2 = "ASIAIOSFODNN7EXAMPLE"
GH_PAT = "ghp_" + "a" * 36


class TestRedactSecretsHappyPath:
    def test_single_match_redacted(self):
        redacted, fired = redact_secrets(f"creds: {AWS_KEY} in config")
        assert AWS_KEY not in redacted
        assert redacted == "creds: [REDACTED:aws_access_key] in config"
        assert fired == ["aws_access_key"]

    def test_clean_content_untouched(self):
        content = "just a normal note about pytest fixtures"
        redacted, fired = redact_secrets(content)
        assert redacted == content
        assert fired == []

    def test_empty_content(self):
        assert redact_secrets("") == ("", [])


class TestRedactSecretsEdgeCases:
    def test_every_occurrence_replaced_and_names_deduped(self):
        # Two AWS keys (long-lived + STS) → both replaced, name fires once.
        redacted, fired = redact_secrets(f"a={AWS_KEY} b={AWS_KEY_2}")
        assert AWS_KEY not in redacted
        assert AWS_KEY_2 not in redacted
        assert redacted.count("[REDACTED:aws_access_key]") == 2
        assert fired == ["aws_access_key"]

    def test_multiple_patterns_ordered_by_pattern_list(self):
        # github_pat comes after aws_access_key in _SECRET_PATTERNS even
        # though it appears first in the content.
        redacted, fired = redact_secrets(f"pat={GH_PAT} then key={AWS_KEY}")
        assert fired == ["aws_access_key", "github_pat"]
        assert "[REDACTED:github_pat]" in redacted
        assert "[REDACTED:aws_access_key]" in redacted

    def test_no_secret_bytes_survive(self):
        secrets = [AWS_KEY, GH_PAT, "glpat-" + "x" * 20]
        redacted, fired = redact_secrets(" ".join(secrets))
        for s in secrets:
            assert s not in redacted
        assert len(fired) == 3

    def test_scan_for_secrets_unchanged(self):
        # Redaction is additive: the existing gate keeps its
        # first-match-name contract.
        assert scan_for_secrets(f"x {AWS_KEY}") == "aws_access_key"
        assert scan_for_secrets("clean") is None
