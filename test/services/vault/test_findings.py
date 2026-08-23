"""Tests for the closed vault finding vocabulary."""

import pytest

from cli_agent_orchestrator.services.vault.findings import (
    FINDING_SEVERITIES,
    SUPPORTED_BOUNDARY,
    FindingCode,
    finding_severity,
)


def test_boundary_table_is_data_for_every_finding_code():
    table_codes = {
        rule.finding_code for rule in SUPPORTED_BOUNDARY if rule.finding_code is not None
    }

    assert set(FindingCode) == table_codes


def test_secret_detected_severity_depends_on_secret_gate():
    assert finding_severity(FindingCode.SECRET_DETECTED, secret_gate="reject") == "error"
    assert finding_severity(FindingCode.SECRET_DETECTED, secret_gate="warn") == "warn"
    assert FindingCode.SECRET_DETECTED not in FINDING_SEVERITIES
    with pytest.raises(TypeError):
        finding_severity(FindingCode.SECRET_DETECTED)


def test_every_non_secret_finding_has_its_adr_severity():
    expected = {
        FindingCode.HEADING_FRAGMENT_IGNORED: "info",
        FindingCode.BLOCK_REFERENCE_UNSUPPORTED: "info",
        FindingCode.EMBED_NOT_INLINED: "info",
        FindingCode.ATTACHMENT_IGNORED: "info",
        FindingCode.ALIAS_AMBIGUOUS: "warn",
        FindingCode.LINK_AMBIGUOUS: "warn",
        FindingCode.KEY_COLLISION: "error",
        FindingCode.LINK_EXCLUDED: "info",
        FindingCode.LINK_DANGLING: "info",
        FindingCode.FRONTMATTER_MALFORMED: "error",
        FindingCode.FRONTMATTER_UNSAFE: "error",
        FindingCode.FRONTMATTER_TOO_LARGE: "error",
        FindingCode.INVALID_CAO_BLOCK: "error",
        FindingCode.KEY_INVALID: "error",
        FindingCode.NOTE_TOO_LARGE: "warn",
        FindingCode.PLUGIN_FORMAT_EXCLUDED: "info",
        FindingCode.SYMLINK_REFUSED: "error",
        FindingCode.SYNC_ARTIFACT_SKIPPED: "info",
        FindingCode.PATH_CASE_COLLISION: "error",
        FindingCode.HARDLINK_REFUSED: "warn",
        FindingCode.PATH_ESCAPES_ROOT: "warn",
        FindingCode.NOTE_NOT_UTF8: "error",
        FindingCode.UNSTABLE_SKIPPED: "warn",
        FindingCode.LINK_LIMIT_EXCEEDED: "warn",
        FindingCode.LINK_TARGET_INVALID: "warn",
        FindingCode.BYTE_BUDGET_EXCEEDED: "warn",
        FindingCode.NOTE_LIMIT_EXCEEDED: "warn",
    }

    assert dict(FINDING_SEVERITIES) == expected
    assert set(FINDING_SEVERITIES) == set(FindingCode) - {FindingCode.SECRET_DETECTED}
    with pytest.raises(TypeError):
        FINDING_SEVERITIES[FindingCode.SYMLINK_REFUSED] = "info"  # type: ignore[index]
