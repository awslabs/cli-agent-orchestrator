"""U1: canonical launch-refusal exception and its compatibility alias.

Traces to FR-101 (enabling), ADR-002, ADR-005, BR-U1-1..8.
"""

import pytest

from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.kiro_launch import (
    LEGACY_REFUSAL_CODE,
    KiroLaunchRefusedError,
)
from cli_agent_orchestrator.providers import kiro_capabilities
from cli_agent_orchestrator.providers.kiro_capabilities import KiroPhase0KASError


def test_alias_is_the_same_class_object_in_both_directions() -> None:
    """BR-U1-2/BR-U1-3: one class, two names — not a subclass."""
    assert KiroPhase0KASError is KiroLaunchRefusedError
    assert KiroLaunchRefusedError is kiro_capabilities.KiroPhase0KASError


def test_legacy_handler_catches_instance_raised_under_canonical_name() -> None:
    """BR-U1-2: an `except KiroPhase0KASError` block still binds.

    A subclass would NOT catch a parent instance, which is exactly the silent
    handler-bypass the alias makes structurally impossible.
    """
    try:
        raise KiroLaunchRefusedError(code="profile-untranslatable")
    except KiroPhase0KASError as exc:
        caught = exc
    assert isinstance(caught, KiroLaunchRefusedError)
    assert isinstance(caught, KiroPhase0KASError)


def test_exception_subclasses_value_error() -> None:
    """BR-U1-3: broad `except ValueError` handlers keep working."""
    assert issubclass(KiroLaunchRefusedError, ValueError)
    assert isinstance(KiroLaunchRefusedError(), ValueError)


@pytest.mark.parametrize("legacy_value", [True, False])
def test_legacy_construction_works_by_keyword(legacy_value: bool) -> None:
    """BR-U1-4: the six keyword-form call sites keep constructing."""
    exc = KiroPhase0KASError(profile_has_v2_policy=legacy_value)
    assert exc.code == LEGACY_REFUSAL_CODE
    assert exc.engine == KiroEngine.KAS
    assert "Phase 0" in str(exc)
    assert ("allowedTools/toolsSettings" in str(exc)) is legacy_value


@pytest.mark.parametrize("legacy_value", [True, False])
def test_legacy_construction_works_positionally(legacy_value: bool) -> None:
    """BR-U1-4: `terminal_service.create_terminal` passes the flag positionally."""
    exc = KiroLaunchRefusedError(legacy_value)
    assert exc.profile_has_v2_policy is legacy_value
    assert "Cedar" in str(exc)


@pytest.mark.parametrize(
    "construct",
    [
        pytest.param(lambda: KiroLaunchRefusedError(), id="bare"),
        pytest.param(lambda: KiroLaunchRefusedError(True), id="legacy-positional"),
        pytest.param(
            lambda: KiroLaunchRefusedError(profile_has_v2_policy=True), id="legacy-keyword"
        ),
        pytest.param(lambda: KiroLaunchRefusedError(code="launch-not-enabled"), id="code-only"),
        pytest.param(
            lambda: KiroLaunchRefusedError(code="profile-untranslatable", message=None),
            id="explicit-message-none",
        ),
        pytest.param(
            lambda: KiroLaunchRefusedError(
                code="profile-untranslatable", profile_field="toolAliases"
            ),
            id="code-and-field",
        ),
        pytest.param(
            lambda: KiroLaunchRefusedError(code="x", engine=KiroEngine.V2), id="non-kas-engine"
        ),
    ],
)
def test_str_is_always_a_meaningful_human_message(construct) -> None:
    """BR-U1-5: `str(exc)` is never empty, never "None", never a repr.

    `super().__init__(None)` yields the literal 4-character string "None", which
    would make every generic logging handler emit it instead of an explanation.
    """
    rendered = str(construct())
    assert rendered
    assert rendered != "None"
    assert not rendered.startswith("KiroLaunchRefusedError(")
    assert "\n" not in rendered.strip()


def test_explicit_message_is_used_verbatim() -> None:
    """BR-U1-5: a caller-supplied message is not rewritten."""
    exc = KiroLaunchRefusedError(code="launch-not-enabled", message="Set CAO_ENABLE_KAS_LAUNCH.")
    assert str(exc) == "Set CAO_ENABLE_KAS_LAUNCH."
    assert exc.message == "Set CAO_ENABLE_KAS_LAUNCH."


def test_structured_fields_are_readable_and_profile_field_may_be_none() -> None:
    """BR-U1-6/BR-U1-7: code, profile_field, engine are the rendering inputs."""
    attributed = KiroLaunchRefusedError(
        code="unsupported-settings", profile_field="toolsSettings", engine=KiroEngine.KAS
    )
    assert attributed.code == "unsupported-settings"
    assert attributed.profile_field == "toolsSettings"
    assert attributed.engine == KiroEngine.KAS

    ambiguous = KiroLaunchRefusedError(code="unknown-capability")
    assert ambiguous.profile_field is None


def test_canonical_module_does_not_import_providers() -> None:
    """BR-U1-1: the layering assertion — no `utils/models -> providers` edge."""
    from pathlib import Path

    import cli_agent_orchestrator.models.kiro_launch as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "cli_agent_orchestrator.providers" not in source
    assert "cli_agent_orchestrator.services" not in source
