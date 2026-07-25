"""U5: diagnostic-code to profile-field attribution and its drift guard.

Traces to FR-104, ADR-009, BR-U5-1..7.
"""

import pathlib
import re

import pytest

import cli_agent_orchestrator
from cli_agent_orchestrator.utils.kiro_launch_guard import _CODE_TO_FIELD

# Matches every ``KiroPolicyError("<code>", ...)`` construction in the source.
_RAISE_RE = re.compile(r"""KiroPolicyError\(\s*["']([a-z0-9-]+)["']""")

_SRC_ROOT = pathlib.Path(cli_agent_orchestrator.__file__).resolve().parent


def _codes_raised_in_src() -> dict[str, set[str]]:
    """Collect every diagnostic code raised anywhere under ``src/``.

    Deliberately scans a **directory**, never an enumerated file list: two codes
    (``unknown-resource``, ``contradictory-resource``) are raised in
    ``services/kiro_profiles.py``, not in ``utils/kiro_policy.py``. A scan scoped
    to the compiler would pass permanently while leaving them unverified — the
    most dangerous outcome for a completeness invariant (BR-U5-1).
    """
    found: dict[str, set[str]] = {}
    for path in _SRC_ROOT.rglob("*.py"):
        for match in _RAISE_RE.finditer(path.read_text(encoding="utf-8")):
            found.setdefault(match.group(1), set()).add(str(path.relative_to(_SRC_ROOT)))
    return found


def test_every_diagnostic_code_in_src_is_a_table_key() -> None:
    """BR-U5-1/BR-U5-7: build-failing completeness drift guard."""
    raised = _codes_raised_in_src()
    assert raised, "the source scan found no KiroPolicyError codes — the scan itself is broken"

    unmapped = {code: sorted(files) for code, files in raised.items() if code not in _CODE_TO_FIELD}
    assert not unmapped, (
        "FR-104/ADR-009: every KiroPolicyError code must appear as a key in "
        "_CODE_TO_FIELD (mapped to a field, or explicitly None). Missing: "
        f"{unmapped}"
    )


def test_scan_covers_modules_outside_the_policy_compiler() -> None:
    """BR-U5-1: guards the scan's scope, not just its result.

    If someone narrows the scan to ``utils/kiro_policy.py``, this fails — the
    resource codes live in ``services/kiro_profiles.py``.
    """
    raised = _codes_raised_in_src()
    assert "unknown-resource" in raised
    assert "contradictory-resource" in raised
    outside = {
        file for code in ("unknown-resource", "contradictory-resource") for file in raised[code]
    }
    assert any("kiro_profiles.py" in file for file in outside)


def test_malformed_cedar_rule_is_declared_and_unattributed() -> None:
    """BR-U5-2/BR-U4-8: declared proactively; internal, so no field."""
    assert "malformed-cedar-rule" in _CODE_TO_FIELD
    assert _CODE_TO_FIELD["malformed-cedar-rule"] is None


@pytest.mark.parametrize("code", ["unknown-capability", "contradictory-policy", "malformed-policy"])
def test_ambiguous_codes_map_to_none_never_a_guess(code: str) -> None:
    """BR-U5-3: allowedTools | deniedTools | tools are indistinguishable here."""
    assert code in _CODE_TO_FIELD
    assert _CODE_TO_FIELD[code] is None


@pytest.mark.parametrize(
    ("code", "field"),
    [
        ("unsafe-aliases", "toolAliases"),
        ("unsupported-settings", "toolsSettings"),
        ("unknown-mcp-server", "mcpServers"),
        ("unsafe-mcp-grant", "mcpServers"),
        ("malformed-mcp", "mcpServers"),
        ("unsupported-mcp-field", "mcpServers"),
        ("unknown-role", "role"),
        ("malformed-role-policy", "role"),
        ("unknown-resource", "resources"),
        ("contradictory-resource", "resources"),
        ("serialization-error", None),
    ],
)
def test_known_mappings(code: str, field: str | None) -> None:
    assert _CODE_TO_FIELD[code] == field


def test_lookup_of_an_unknown_code_returns_none_without_raising() -> None:
    """BR-U5-4: attribution is best-effort; consumers handle absence."""
    assert _CODE_TO_FIELD.get("code-that-does-not-exist") is None


def test_table_values_are_field_names_never_runtime_values() -> None:
    """SEC-U5-1: the table cannot leak profile content because it holds no values."""
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile_fields = set(AgentProfile.model_fields)
    for code, field in _CODE_TO_FIELD.items():
        assert (
            field is None or field in profile_fields
        ), f"{code!r} maps to {field!r}, which is not an AgentProfile field name"
