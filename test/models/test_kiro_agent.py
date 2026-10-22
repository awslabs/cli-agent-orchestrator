"""``KiroAgentConfig`` must serialize the MCP-json flag under Kiro CLI's current name.

Kiro CLI retired ``useLegacyMcpJson`` in favour of ``includeMcpJson`` and keeps the
old spelling as an alias of the same field, so a spec carrying BOTH is rejected
with ``duplicate field includeMcpJson`` (#1026). The field is non-Optional, so the
writer's ``exclude_none=True`` cannot drop it and every installed spec carries it,
which is why the emitted spelling has to be the canonical one.

Two halves, both pinned here: the OUTPUT is canonical-only (including under
``by_alias=True``), and the retired INPUT spelling still loads. The last class
guards the class of defect rather than the instance: no field of the model may be
declared under a spelling Kiro CLI has retired, so the alias cannot quietly come
back as a field name in a later edit.

Test shape and the first eight cases follow the patch proposal @ekson73 posted on
#1026.
"""

import json

import pytest

from cli_agent_orchestrator.models.kiro_agent import KiroAgentConfig

# Spellings Kiro CLI has retired. Kiro still accepts them as aliases on read, so
# emitting one next to its canonical name produces a duplicate-field rejection.
# Add to this set when Kiro retires another; TestNoRetiredSpellingIsAField then
# fails on any model field that uses it.
RETIRED_KIRO_SPEC_KEYS = frozenset({"useLegacyMcpJson"})


def _rendered(cfg: KiroAgentConfig) -> dict:
    """The spec exactly as install_service writes it to ``~/.kiro/agents``."""
    return json.loads(cfg.model_dump_json(indent=2, exclude_none=True))


class TestKiroAgentConfigMcpJsonField:
    def test_serializes_canonical_name_only(self):
        rendered = _rendered(KiroAgentConfig(name="a", description="d"))
        assert rendered["includeMcpJson"] is False
        assert "useLegacyMcpJson" not in rendered

    def test_serializes_canonical_name_when_enabled(self):
        rendered = _rendered(KiroAgentConfig(name="a", description="d", includeMcpJson=True))
        assert rendered["includeMcpJson"] is True
        assert "useLegacyMcpJson" not in rendered

    def test_model_dump_by_alias_still_emits_canonical_name(self):
        """The alias is validation-only: ``by_alias=True`` must not resurrect it."""
        dumped = KiroAgentConfig(name="a", description="d").model_dump(by_alias=True)
        assert "includeMcpJson" in dumped
        assert "useLegacyMcpJson" not in dumped

    def test_legacy_input_name_still_loads(self):
        """Profiles, fixtures and raw specs written with the retired name keep working."""
        cfg = KiroAgentConfig.model_validate(
            {"name": "a", "description": "d", "useLegacyMcpJson": True}
        )
        assert cfg.includeMcpJson is True

    def test_legacy_input_name_round_trips_to_canonical_output(self):
        cfg = KiroAgentConfig.model_validate(
            {"name": "a", "description": "d", "useLegacyMcpJson": True}
        )
        rendered = _rendered(cfg)
        assert rendered["includeMcpJson"] is True
        assert "useLegacyMcpJson" not in rendered

    def test_canonical_input_name_loads(self):
        cfg = KiroAgentConfig.model_validate(
            {"name": "a", "description": "d", "includeMcpJson": True}
        )
        assert cfg.includeMcpJson is True

    def test_keyword_construction_accepts_both_spellings(self):
        assert KiroAgentConfig(name="a", description="d", includeMcpJson=True).includeMcpJson
        assert KiroAgentConfig(name="a", description="d", useLegacyMcpJson=True).includeMcpJson

    def test_default_is_false(self):
        assert KiroAgentConfig(name="a", description="d").includeMcpJson is False

    def test_both_spellings_on_input_do_not_produce_both_on_output(self):
        """The exact shape the issue reports, fed back in: one key comes out."""
        cfg = KiroAgentConfig.model_validate(
            {"name": "a", "description": "d", "useLegacyMcpJson": False, "includeMcpJson": True}
        )
        rendered = _rendered(cfg)
        assert set(rendered) & {"includeMcpJson", "useLegacyMcpJson"} == {"includeMcpJson"}


class TestNoRetiredSpellingIsAField:
    """The class guard: a retired spelling may be an input alias, never a field.

    A field is what gets serialized. If a later edit re-declares one under a
    retired name, every CAO-installed spec carries it again and the duplicate-field
    rejection returns the moment another writer adds the canonical key.
    """

    def test_the_guard_has_something_to_check(self):
        assert RETIRED_KIRO_SPEC_KEYS, "an empty retired set makes the guard below vacuous"

    @pytest.mark.parametrize("retired", sorted(RETIRED_KIRO_SPEC_KEYS))
    def test_retired_spelling_is_not_a_model_field(self, retired: str):
        assert retired not in KiroAgentConfig.model_fields

    @pytest.mark.parametrize("retired", sorted(RETIRED_KIRO_SPEC_KEYS))
    def test_retired_spelling_never_reaches_the_rendered_spec(self, retired: str):
        cfg = KiroAgentConfig.model_validate({"name": "a", "description": "d", retired: True})
        assert retired not in _rendered(cfg)
        assert retired not in cfg.model_dump(by_alias=True)
