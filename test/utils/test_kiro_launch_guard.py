"""U3: the single KAS launch-admissibility decision point.

Traces to FR-101, FR-102, FR-104, ADR-001/003/008, BR-U3-1..11.

Flag state is always set with ``monkeypatch.setattr`` on the **resolved
constant** — never by mutating the environment. ``ENABLE_KAS_LAUNCH`` is read
once at import, so a late env mutation silently no-ops and the test would assert
the wrong state while passing green (SEC-U9-8).
"""

import pathlib
import re

import pytest

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.kiro_launch import KiroLaunchRefusedError
from cli_agent_orchestrator.utils import kiro_launch_guard
from cli_agent_orchestrator.utils.kiro_launch_guard import (
    assert_kas_launch_allowed,
    check_kas_launch,
)
from cli_agent_orchestrator.utils.kiro_policy import CompiledKiroPolicy


@pytest.fixture
def kas_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(constants, "ENABLE_KAS_LAUNCH", True)


@pytest.fixture
def kas_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(constants, "ENABLE_KAS_LAUNCH", False)


def _translatable() -> AgentProfile:
    return AgentProfile(
        name="guard-ok",
        description="Translatable",
        engine=KiroEngine.KAS,
        allowedTools=["fs_read"],
    )


def _untranslatable(**kwargs) -> AgentProfile:
    base = {
        "name": "guard-bad",
        "description": "Untranslatable",
        "engine": KiroEngine.KAS,
        "allowedTools": ["fs_read"],
        "toolsSettings": {"fs_read": {"allowedPaths": ["/synthetic"]}},
    }
    base.update(kwargs)
    return AgentProfile(**base)


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize("with_profile", [True, False])
def test_non_kas_engine_passes_through_untouched(
    monkeypatch: pytest.MonkeyPatch, flag: bool, with_profile: bool
) -> None:
    """BR-U3-6: v2 cannot be affected by a KAS regression."""
    monkeypatch.setattr(constants, "ENABLE_KAS_LAUNCH", flag)
    profile = _untranslatable(engine=None) if with_profile else None

    verdict = check_kas_launch(engine=KiroEngine.V2, profile=profile)

    assert verdict.allowed is True
    assert verdict.engine == KiroEngine.V2
    assert verdict.reason_code is None
    assert_kas_launch_allowed(engine=KiroEngine.V2, profile=profile)


def test_flag_off_refuses_without_a_profile(kas_disabled: None) -> None:
    """BR-U3-2: default refuses; KAS never becomes reachable by omission."""
    verdict = check_kas_launch(engine=KiroEngine.KAS)

    assert verdict.allowed is False
    assert verdict.reason_code == "launch-not-enabled"
    assert verdict.mode == "flag-only"
    assert verdict.profile_field is None
    assert "CAO_ENABLE_KAS_LAUNCH" in (verdict.message or "")


def test_flag_off_refuses_with_a_profile_before_any_compile(
    kas_disabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag precedes lint, so a flag-off refusal costs no policy compile.

    ``mode`` still reports ``lint-gated`` because it names the *kind of call
    site* (creation), not which checks happened to run.
    """

    def explode(*_args, **_kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("lint must not run when the opt-in flag is off")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.kiro_profile_lint.lint_kiro_profile", explode
    )

    verdict = check_kas_launch(engine=KiroEngine.KAS, profile=_translatable())

    assert verdict.allowed is False
    assert verdict.reason_code == "launch-not-enabled"
    assert verdict.mode == "lint-gated"


def test_flag_on_without_a_profile_is_allowed_in_flag_only_mode(kas_enabled: None) -> None:
    """BR-U3-5: a missing profile is not a refusal (ADR-008)."""
    verdict = check_kas_launch(engine=KiroEngine.KAS)

    assert verdict.allowed is True
    assert verdict.mode == "flag-only"
    assert verdict.policy is None
    assert verdict.reason_code is None


def test_flag_on_with_a_translatable_profile_is_lint_gated_and_carries_the_policy(
    kas_enabled: None,
) -> None:
    verdict = check_kas_launch(engine=KiroEngine.KAS, profile=_translatable())

    assert verdict.allowed is True
    assert verdict.mode == "lint-gated"
    assert isinstance(verdict.policy, CompiledKiroPolicy)
    assert verdict.policy.visible_tools == ("fs_read",)


def test_flag_on_with_an_untranslatable_profile_refuses_with_attribution(
    kas_enabled: None,
) -> None:
    """BR-U3-7/BR-U3-8: the lint verdict is the oracle; the refusal is actionable."""
    verdict = check_kas_launch(engine=KiroEngine.KAS, profile=_untranslatable())

    assert verdict.allowed is False
    assert verdict.mode == "lint-gated"
    assert verdict.reason_code == "profile-untranslatable"
    assert verdict.profile_field == "toolsSettings"
    assert "unsupported-settings" in (verdict.message or "")


def test_ambiguous_diagnostic_yields_no_field_but_an_actionable_message(
    kas_enabled: None,
) -> None:
    """ADR-009: no guess — the message still quotes the offending token."""
    profile = AgentProfile(
        name="guard-ambiguous",
        description="Ambiguous",
        engine=KiroEngine.KAS,
        allowedTools=["not_a_real_tool"],
    )

    verdict = check_kas_launch(engine=KiroEngine.KAS, profile=profile)

    assert verdict.allowed is False
    assert verdict.reason_code == "profile-untranslatable"
    assert verdict.profile_field is None
    assert "not_a_real_tool" in (verdict.message or "")


def test_verdict_is_recomputed_between_calls(kas_enabled: None) -> None:
    """BR-U3-3/ADR-003: mutating the profile changes the verdict — no cache.

    A cached verdict would launch a profile edited between install and launch
    against a stale "safe" answer.
    """
    profile = _translatable()

    first = check_kas_launch(engine=KiroEngine.KAS, profile=profile)
    assert first.allowed is True

    profile.toolAliases = {"ls": "fs_list"}
    second = check_kas_launch(engine=KiroEngine.KAS, profile=profile)

    assert second.allowed is False
    assert second.profile_field == "toolAliases"

    profile.toolAliases = None
    third = check_kas_launch(engine=KiroEngine.KAS, profile=profile)
    assert third.allowed is True


def _guard_code_without_prose() -> str:
    """Return the guard's source with comments and docstrings stripped.

    The module *documents* why caching is forbidden, so a naive substring scan
    would match its own prose. Only executable code is inspected.
    """
    import io
    import tokenize

    source = pathlib.Path(kiro_launch_guard.__file__).read_text(encoding="utf-8")
    kept: list[str] = []
    previous = tokenize.INDENT
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and previous in (
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.ENCODING,
        ):
            continue  # a docstring
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            kept.append(token.string)
        previous = token.type
    return " ".join(kept)


def test_guard_declares_no_cache_of_any_kind() -> None:
    """BR-U3-4: statelessness is the mechanism, so assert its absence."""
    code = _guard_code_without_prose()
    for forbidden in ("lru_cache", "cache", "memo", "_VERDICT"):
        assert forbidden not in code, (
            f"ADR-003 forbids caching a translatability verdict; found {forbidden!r} "
            "in the guard's executable code"
        )
    module_attrs = {
        name
        for name, value in vars(kiro_launch_guard).items()
        if isinstance(value, (dict, list, set)) and not name.startswith("__")
    }
    assert module_attrs == {"_CODE_TO_FIELD"}, (
        "the guard must hold no mutable module state beyond the static attribution "
        f"table; found: {sorted(module_attrs)}"
    )


def test_a_malformed_profile_never_yields_allowed_true(
    kas_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No try/except may resolve an error to allowed=True."""

    def broken_lint(*_args, **_kwargs):
        raise RuntimeError("synthetic lint failure")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.kiro_profile_lint.lint_kiro_profile", broken_lint
    )

    with pytest.raises(RuntimeError, match="synthetic lint failure"):
        check_kas_launch(engine=KiroEngine.KAS, profile=_translatable())


def test_assert_wrapper_raises_exactly_when_the_verdict_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(constants, "ENABLE_KAS_LAUNCH", False)
    with pytest.raises(KiroLaunchRefusedError) as exc_info:
        assert_kas_launch_allowed(engine=KiroEngine.KAS)
    assert exc_info.value.code == "launch-not-enabled"
    assert exc_info.value.engine == KiroEngine.KAS
    assert str(exc_info.value)
    assert str(exc_info.value) != "None"

    monkeypatch.setattr(constants, "ENABLE_KAS_LAUNCH", True)
    assert assert_kas_launch_allowed(engine=KiroEngine.KAS) is None


def test_assert_wrapper_propagates_attribution(kas_enabled: None) -> None:
    with pytest.raises(KiroLaunchRefusedError) as exc_info:
        assert_kas_launch_allowed(engine=KiroEngine.KAS, profile=_untranslatable())
    assert exc_info.value.code == "profile-untranslatable"
    assert exc_info.value.profile_field == "toolsSettings"


def test_guard_has_no_module_level_services_import() -> None:
    """BR-U3-9: the `utils -> services` edge stays inside the one function."""
    source = pathlib.Path(kiro_launch_guard.__file__).read_text(encoding="utf-8")
    module_level = [
        line
        for line in source.splitlines()
        if re.match(r"^(from|import)\s", line) and "cli_agent_orchestrator.services" in line
    ]
    assert module_level == [], (
        "ADR-001: the launch guard must import services lazily inside the "
        f"function, not at module level; found: {module_level}"
    )
    assert "from cli_agent_orchestrator.services.kiro_profile_lint import" in source
