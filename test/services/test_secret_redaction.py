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


# Assembled from short pieces so no quoted run reads as a credential to a scanner.
_AWS_40_CHAR_SAMPLE = "wJalrXUtn" + "FEMI/K7MD" + "ENG/bPxRf" + "iCYEXAMPL" + "EKEY"
_LONG_VALUE = "hunter2long" + "enough12345"


class TestRedactJsonLeavesKeepsKeyContext:
    """``redact_json_leaves`` scans keys and values apart; the key IS the context."""

    def test_secret_access_key_value_is_redacted_whole(self):
        from cli_agent_orchestrator.services.secret_gate import redact_json_leaves

        out = redact_json_leaves(
            {"Credentials": {"SecretAccessKey": _AWS_40_CHAR_SAMPLE, "Expiration": "t"}}
        )
        assert out == {
            "Credentials": {
                "SecretAccessKey": "[REDACTED:aws_secret_access_key]",
                "Expiration": "t",
            }
        }

    def test_snake_case_key_counts_too(self):
        from cli_agent_orchestrator.services.secret_gate import redact_json_leaves

        out = redact_json_leaves({"aws_secret_access_key": _AWS_40_CHAR_SAMPLE})
        assert out == {"aws_secret_access_key": "[REDACTED:aws_secret_access_key]"}

    def test_environment_entry_value_is_redacted_and_name_kept(self):
        from cli_agent_orchestrator.services.secret_gate import redact_json_leaves

        entries = [
            {"name": "AWS_REGION", "value": "us-east-1"},
            {"name": "AWS_SECRET_ACCESS_KEY", "value": _AWS_40_CHAR_SAMPLE},
        ]
        assert redact_json_leaves(entries) == [
            {"name": "AWS_REGION", "value": "us-east-1"},
            {"name": "AWS_SECRET_ACCESS_KEY", "value": "[REDACTED:aws_secret_access_key]"},
        ]

    def test_generic_credential_keys_redact_their_values(self):
        from cli_agent_orchestrator.services.secret_gate import redact_json_leaves

        out = redact_json_leaves(
            {
                "password": "correct horse battery staple",
                "api_key": _LONG_VALUE,
                "user": "ted",
            }
        )
        assert out == {
            "password": "[REDACTED:secret_assignment]",
            "api_key": "[REDACTED:bearer_token]",
            "user": "ted",
        }

    def test_forty_character_run_under_another_key_is_left_alone(self):
        from cli_agent_orchestrator.services.secret_gate import redact_json_leaves

        doc = {
            "commit": "a" * 40,
            "build_id": _AWS_40_CHAR_SAMPLE,
            "entries": [{"name": "BUILD", "value": _AWS_40_CHAR_SAMPLE}],
        }
        assert redact_json_leaves(doc) == doc

    def test_step_output_sanitiser_uses_the_key_context(self):
        import json

        from cli_agent_orchestrator.services.script_runner import _sanitise_output_json

        raw = json.dumps({"sts": {"SecretAccessKey": _AWS_40_CHAR_SAMPLE}}, separators=(",", ":"))
        cleaned = _sanitise_output_json(raw)
        assert cleaned is not None
        assert _AWS_40_CHAR_SAMPLE not in cleaned
        assert json.loads(cleaned) == {
            "sts": {"SecretAccessKey": "[REDACTED:aws_secret_access_key]"}
        }


class TestRedactZeroWidth:
    def test_hidden_credential_is_redacted_whole(self):
        redacted, fired = redact_secrets("k=AK\u200bIAIOSFODNN7EXAMPLE")
        assert redacted == "k=[REDACTED:aws_access_key]"
        assert fired == ["aws_access_key"]
        assert "IOSFODNN7EXAMPLE" not in redacted

    def test_emoji_elsewhere_survives_when_a_credential_is_redacted(self):
        # The joiner inside the emoji family is outside the credential span and
        # must not be dropped just because a hidden credential sits nearby.
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        redacted, fired = redact_secrets(f"family {family} key AK\u200bIAIOSFODNN7EXAMPLE")
        assert redacted == f"family {family} key [REDACTED:aws_access_key]"
        assert fired == ["aws_access_key"]

    def test_clean_content_keeps_zero_width_characters(self):
        # U+200D is the joiner inside emoji family sequences; clean text must
        # come back byte-for-byte.
        content = "family \U0001f468\u200d\U0001f469\u200d\U0001f467 ok"
        redacted, fired = redact_secrets(content)
        assert redacted == content
        assert fired == []


class TestRedactVendorFamilies:
    def test_each_new_family_is_replaced_by_its_own_marker(self):
        jwt = ".".join(
            [
                "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
                "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            ]
        )
        secrets = {
            "anthropic_api_key": "sk-ant-api03-" + "Qz7" * 30,
            "openai_api_key": "sk-proj-" + "Ab9" * 20,
            "github_fine_grained_pat": "github_pat_" + "A" * 22 + "_" + "b" * 59,
            "slack_token": "xox" + "b-1234567890-1234567890-AbCdEfGhIjKlMnOp",
            "jwt": jwt,
        }
        redacted, fired = redact_secrets(" ".join(secrets.values()))
        for name, value in secrets.items():
            assert value not in redacted, name
            assert f"[REDACTED:{name}]" in redacted, name
        assert fired == list(secrets)  # _SECRET_PATTERNS order

    def test_aws_secret_key_context_form(self):
        redacted, fired = redact_secrets("export AWS_SECRET_ACCESS_KEY=" + _AWS_40_CHAR_SAMPLE)
        assert "wJalrXUtnFEMI" not in redacted
        assert fired == ["aws_secret_access_key"]
