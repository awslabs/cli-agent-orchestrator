"""The ephemeral provider-capability table and its invariants (#1074 phase 3).

Class names and refusal strings are provisional (#1074 open questions 4 and 6);
these tests check properties of the table, not its wording.
"""

from dataclasses import replace

import pytest

from cli_agent_orchestrator.models.ephemeral import TOOL_ATOMS
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.utils import ephemeral_capabilities as caps
from cli_agent_orchestrator.utils.enforcement import NATIVE, PROVIDER_ENFORCEMENT


def test_claude_code_is_hard_with_every_atom_model_and_effort():
    row = caps.capability_for("claude_code")
    assert row.supports_ephemeral and row.enforcement == caps.HARD
    assert row.tool_atoms == frozenset(TOOL_ATOMS)
    assert row.honors_model and row.honors_effort
    assert row.effort_config[0] == "claudeConfig"


def test_codex_is_advisory_until_the_contained_launch():
    # Codex runs --yolo with network on, so web_fetch is not refused yet (#1074).
    row = caps.capability_for("codex")
    assert row.supports_ephemeral and row.enforcement == caps.ADVISORY
    assert row.tool_atoms == frozenset(TOOL_ATOMS)
    assert row.honors_model and row.honors_effort
    assert row.effort_config[0] == "codexConfig"


def test_kiro_is_refused_until_its_ephemeral_launch():
    row = caps.capability_for("kiro_cli")
    assert not row.supports_ephemeral and row.enforcement == caps.REFUSED
    assert not row.tool_atoms and not row.honors_model and not row.honors_effort


@pytest.mark.parametrize("provider", [p.value for p in ProviderType])
def test_every_provider_outside_the_table_is_refused(provider):
    row = caps.capability_for(provider)
    if provider not in caps.CAPABILITIES:
        assert row.enforcement == caps.REFUSED and not row.tool_atoms
    assert row.supports_ephemeral == (provider in caps.ephemeral_providers())


@pytest.mark.parametrize("value", ["", "Claude_Code", "not-a-provider", None, 1, ["codex"], {}])
def test_unknown_and_non_string_providers_fail_closed(value):
    assert caps.capability_for(value).enforcement == caps.REFUSED
    assert caps.unsupported_atoms(value, ["fs_read"]) == ["fs_read"]


def test_ephemeral_providers_are_the_supporting_rows_in_table_order():
    assert caps.ephemeral_providers() == ("claude_code", "codex")
    assert set(caps.ephemeral_providers()) <= {p.value for p in ProviderType}
    for provider in caps.ephemeral_providers():
        assert caps.CAPABILITIES[provider].supports_ephemeral


def test_a_hard_label_is_backed_by_native_installed_enforcement():
    # The ephemeral label may never claim more than utils/enforcement.py does.
    for provider in caps.ephemeral_providers():
        assert provider in PROVIDER_ENFORCEMENT
        if caps.capability_for(provider).enforcement == caps.HARD:
            assert PROVIDER_ENFORCEMENT[provider] == NATIVE, provider


def test_every_supporting_row_has_a_reportable_class():
    for provider in caps.ephemeral_providers():
        assert caps.capability_for(provider).enforcement in set(caps.ENFORCEMENT_CLASSES) - {
            caps.REFUSED
        }


def test_unsupported_atoms_follow_tool_atom_order_and_skip_non_atoms(monkeypatch):
    narrow = replace(
        caps.CAPABILITIES["codex"], tool_atoms=frozenset({"fs_read", "fs_list", "web_fetch"})
    )
    monkeypatch.setitem(caps.CAPABILITIES, "codex", narrow)
    requested = ["web_fetch", "execute_bash", "@cao-mcp-server", "fs_write", "fs_read"]
    # TOOL_ATOMS order, not request or alphabetical order: fs_write precedes execute_bash.
    assert caps.unsupported_atoms("codex", requested) == ["fs_write", "execute_bash"]
    assert caps.unsupported_atoms("codex", ["@cao-mcp-server", "*", "fs_read"]) == []
    assert caps.unsupported_atoms("claude_code", list(TOOL_ATOMS) + ["@cao-mcp-server"]) == []


@pytest.mark.parametrize(
    "changes,message",
    [
        (dict(enforcement="soft"), "unknown enforcement class"),
        (dict(tool_atoms=frozenset({"fs_read", "Bash"})), "tool_atoms"),
        (dict(honors_model=False), "honour model and effort"),
        (dict(effort_config=None), "honour model and effort"),
    ],
)
def test_a_row_that_breaks_an_invariant_is_rejected(changes, message):
    with pytest.raises(ValueError, match=message):
        replace(caps.CAPABILITIES["claude_code"], **changes)


def test_a_refused_row_cannot_carry_atoms():
    with pytest.raises(ValueError, match="refused provider runs no tool atoms"):
        replace(caps.CAPABILITIES["kiro_cli"], tool_atoms=frozenset({"fs_read"}))


def test_the_reserved_sandboxed_class_is_a_valid_supporting_row():
    # The shape the contained Codex launch is expected to take: sandboxed, no web_fetch.
    row = replace(
        caps.CAPABILITIES["codex"],
        enforcement=caps.SANDBOXED,
        tool_atoms=frozenset(TOOL_ATOMS) - {"web_fetch"},
    )
    assert row.supports_ephemeral and row.honors_effort
