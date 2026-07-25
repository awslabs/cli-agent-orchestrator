"""U9: FR-101's structural acceptance criteria.

AC-101.a and AC-101.b verify properties of the code's **shape**, not of its
behaviour, and they catch disjoint failures that behaviour cannot see:

| Failure                                              | behavioural | AC-101.a | AC-101.b |
|------------------------------------------------------|-------------|----------|----------|
| 3 of 7 sites keep their old raise                    | passes      | CATCHES  | passes   |
| a site refuses via unrelated validation, no guard    | passes      | passes   | CATCHES  |
| a site no longer refuses at all                      | CATCHES     | passes   | CATCHES  |

If either proves awkward to write, the correct response is to fix the test —
**never** to substitute a behavioural refusal test. They do not test the same
thing, and a behavioural substitute passes on a partial refactor while FR-101's
single-decision-point property is silently unmet.
"""

import asyncio
import pathlib
import re
from unittest.mock import MagicMock, Mock, patch

import pytest

import cli_agent_orchestrator
from cli_agent_orchestrator import constants
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilities
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.providers.manager import ProviderManager

_SRC_ROOT = pathlib.Path(cli_agent_orchestrator.__file__).resolve().parent
_GUARD_MODULE = "utils/kiro_launch_guard.py"

_TERMINAL_SERVICE = "cli_agent_orchestrator.services.terminal_service"
_MANAGER = "cli_agent_orchestrator.providers.manager"

_KAS_METADATA = {
    "id": "kas-terminal",
    "provider": "kiro_cli",
    "engine": "kas",
    "tmux_session": "cao-session",
    "tmux_window": "developer-window",
    "agent_profile": "developer",
}


# ---------------------------------------------------------------------------
# AC-101.a — source scan
# ---------------------------------------------------------------------------


def test_ac_101_a_only_the_guard_raises_the_refusal() -> None:
    """AC-101.a: exactly one raise site, and it is the guard module.

    Scans non-test source for `raise KiroLaunchRefusedError` (and its legacy
    alias). A surviving raise elsewhere means the seven fail-closed boundaries
    were not actually collapsed into one auditable decision point, which is the
    whole point of FR-101 — and no behavioural test can see it.
    """
    raise_re = re.compile(r"raise\s+(?:KiroLaunchRefusedError|KiroPhase0KASError)\b")

    raisers = sorted(
        str(path.relative_to(_SRC_ROOT))
        for path in _SRC_ROOT.rglob("*.py")
        if raise_re.search(path.read_text(encoding="utf-8"))
    )

    assert raisers == [_GUARD_MODULE], (
        "FR-101 (AC-101.a): the KAS launch refusal must be raised in exactly one "
        f"place, {_GUARD_MODULE}. Found raise sites in: {raisers}. A raise "
        "outside the guard means a second, independent admissibility decision "
        "exists — behavioural refusal tests cannot detect this."
    )


def test_ac_101_a_scan_would_detect_a_planted_raise(tmp_path: pathlib.Path) -> None:
    """The scan's own teeth: a stray raise in any src module must be found.

    Guards against the scan silently degrading into a no-op (e.g. a regex that
    stops matching), which would leave AC-101.a permanently green.
    """
    raise_re = re.compile(r"raise\s+(?:KiroLaunchRefusedError|KiroPhase0KASError)\b")
    planted = "def f():\n    raise KiroLaunchRefusedError(code='x')\n"
    assert raise_re.search(planted)

    legacy = "def f():\n    raise KiroPhase0KASError(profile_has_v2_policy=False)\n"
    assert raise_re.search(legacy)

    innocent = "# raising is done by assert_kas_launch_allowed\nreturn None\n"
    assert not raise_re.search(innocent)


def test_only_one_module_defines_launch_admissibility() -> None:
    """FR-104: a single translatability oracle — no competing check exists.

    ``generation_safe`` is produced by ``kiro_profile_lint`` and consumed as an
    *admission decision* only by the guard. The CLI also reads it, but purely to
    print a diagnostic line — that is display, not a second gate — so it is
    listed explicitly rather than silently tolerated by a loose assertion.
    """
    readers = sorted(
        str(path.relative_to(_SRC_ROOT))
        for path in _SRC_ROOT.rglob("*.py")
        if "generation_safe" in path.read_text(encoding="utf-8")
    )
    assert readers == [
        "cli/commands/profile.py",  # display only: `cao profile lint` output
        "services/kiro_profile_lint.py",  # producer
        _GUARD_MODULE,  # the sole admission consumer
    ], (
        "FR-104 requires exactly one translatability oracle: kiro_profile_lint "
        "produces `generation_safe`, the launch guard is its only admission "
        f"consumer, and the CLI reads it only for display. Found: {readers}"
    )


# ---------------------------------------------------------------------------
# AC-101.b — module-level guard routing, per site
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(constants, "ENABLE_KAS_LAUNCH", False)


@pytest.fixture
def guard_spy():
    """Return a factory that patches the guard *at module level* in one module.

    Patching where the name is bound — not the guard's own definition — is what
    detects a path that imported the guard but never calls it. The spy allows the
    call so the site's downstream behaviour is unaffected.
    """
    calls: list[dict] = []

    def record(**kwargs) -> None:
        calls.append(kwargs)

    def factory(module_path: str):
        return calls, patch(f"{module_path}.assert_kas_launch_allowed", side_effect=record)

    return factory


def test_ac_101_b_site1_create_terminal_invokes_the_guard(guard_spy) -> None:
    calls, patcher = guard_spy(_TERMINAL_SERVICE)
    probe = Mock(return_value=KiroCapabilities(version="3.0.0", flags=frozenset({"--v3"})))
    profile = AgentProfile(
        name="kas-profile",
        description="KAS profile",
        engine=KiroEngine.KAS,
        allowedTools=["fs_read"],
    )

    with (
        patcher,
        patch(f"{_TERMINAL_SERVICE}.load_agent_profile", return_value=profile),
        patch(f"{_TERMINAL_SERVICE}.get_backend") as backend,
        patch(f"{_TERMINAL_SERVICE}.db_create_terminal"),
        patch(f"{_TERMINAL_SERVICE}.fifo_manager"),
        patch(f"{_TERMINAL_SERVICE}.provider_manager") as providers,
        patch(f"{_TERMINAL_SERVICE}.generate_terminal_id", return_value="kas00001"),
        patch(f"{_TERMINAL_SERVICE}.generate_session_name", return_value="session"),
        patch(f"{_TERMINAL_SERVICE}.generate_window_name", return_value="w"),
        patch(f"{_TERMINAL_SERVICE}.get_herdr_inbox_service", return_value=None),
    ):
        from cli_agent_orchestrator.services.terminal_service import create_terminal

        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        provider = MagicMock()
        provider.initialize = Mock(return_value=asyncio.sleep(0, result=True))
        provider.shell_baseline = None
        providers.create_provider.return_value = provider

        asyncio.run(
            create_terminal(
                provider="kiro_cli",
                agent_profile="kas-profile",
                new_session=True,
                kiro_capability_probe=probe,
            )
        )

    assert calls, (
        "FR-101 (AC-101.b): create_terminal must invoke the central launch guard; "
        "it was never called."
    )
    assert calls[0]["engine"] == KiroEngine.KAS
    assert calls[0]["profile"] is profile, "site 1 is the lint-gated gate (ADR-008)"


def test_ac_101_b_site2_create_provider_invokes_the_guard(guard_spy) -> None:
    calls, patcher = guard_spy(_MANAGER)
    manager = ProviderManager()

    with patcher, patch(f"{_MANAGER}.KiroCliProvider"):
        manager.create_provider(
            ProviderType.KIRO_CLI.value,
            "t-ac2",
            "s1",
            "w1",
            agent_profile="developer",
            engine=KiroEngine.KAS,
        )

    assert calls, "FR-101 (AC-101.b): manager.create_provider must invoke the guard"
    assert calls[0] == {"engine": KiroEngine.KAS}


def test_ac_101_b_site3_provider_initialize_invokes_the_guard(guard_spy) -> None:
    calls, patcher = guard_spy("cli_agent_orchestrator.providers.kiro_cli")
    provider = KiroCliProvider("t-ac3", "s1", "w1", "developer", engine=KiroEngine.KAS)

    with (
        patcher,
        patch("cli_agent_orchestrator.providers.kiro_cli.wait_for_shell", return_value=False),
        patch("cli_agent_orchestrator.providers.kiro_cli.get_server_settings") as settings,
    ):
        settings.return_value = {"provider_init_timeout": 1}
        with pytest.raises(TimeoutError):
            asyncio.run(provider.initialize())

    assert calls, "FR-101 (AC-101.b): KiroCliProvider.initialize must invoke the guard"
    assert calls[0] == {"engine": KiroEngine.KAS}


def test_ac_101_b_site4_get_provider_cached_invokes_the_guard(guard_spy) -> None:
    calls, patcher = guard_spy(_MANAGER)
    manager = ProviderManager()
    manager._providers["t-ac4"] = KiroCliProvider(
        "t-ac4", "s1", "w1", "developer", engine=KiroEngine.KAS
    )

    with patcher:
        assert manager.get_provider("t-ac4") is manager._providers["t-ac4"]

    assert calls, "FR-101 (AC-101.b): get_provider's cached path must invoke the guard"
    assert calls[0] == {"engine": KiroEngine.KAS}


def test_ac_101_b_site5_get_provider_on_demand_invokes_the_guard(guard_spy) -> None:
    calls, patcher = guard_spy(_MANAGER)
    manager = ProviderManager()

    with (
        patcher,
        patch(f"{_MANAGER}.get_terminal_metadata", return_value=dict(_KAS_METADATA)),
        patch(f"{_MANAGER}.KiroCliProvider"),
    ):
        manager.get_provider("kas-terminal")

    assert calls, "FR-101 (AC-101.b): get_provider's on-demand path must invoke the guard"
    # Called once for the reconstruction check, and again inside create_provider.
    assert calls[0] == {"engine": KiroEngine.KAS}


def test_ac_101_b_site6_send_input_invokes_the_guard(guard_spy) -> None:
    calls, patcher = guard_spy(_TERMINAL_SERVICE)

    with (
        patcher,
        patch(f"{_TERMINAL_SERVICE}.get_terminal_metadata", return_value=dict(_KAS_METADATA)),
        patch(f"{_TERMINAL_SERVICE}.provider_manager") as providers,
        patch(f"{_TERMINAL_SERVICE}.get_backend"),
    ):
        from cli_agent_orchestrator.services.terminal_service import send_input

        providers.get_provider.side_effect = RuntimeError("stop-after-guard")
        with pytest.raises(RuntimeError, match="stop-after-guard"):
            send_input("kas-terminal", "message")

    assert calls, "FR-101 (AC-101.b): send_input must invoke the guard"
    assert calls[0] == {"engine": KiroEngine.KAS}


def test_ac_101_b_site7_agent_step_reuse_invokes_the_guard(guard_spy) -> None:
    calls, patcher = guard_spy("cli_agent_orchestrator.services.agent_step")

    with (
        patcher,
        patch(f"{_TERMINAL_SERVICE}.get_terminal_metadata", return_value=dict(_KAS_METADATA)),
        patch(f"{_TERMINAL_SERVICE}.provider_manager"),
        patch(f"{_TERMINAL_SERVICE}.get_backend"),
        patch(f"{_TERMINAL_SERVICE}.send_input", side_effect=RuntimeError("stop-after-guard")),
    ):
        from cli_agent_orchestrator.services.agent_step import run_agent_step

        with pytest.raises((RuntimeError, Exception), match="stop-after-guard|Kiro"):
            asyncio.run(
                run_agent_step(
                    provider="kiro_cli",
                    agent="developer",
                    prompt="message",
                    reuse_terminal_id="kas-terminal",
                    engine=KiroEngine.KAS,
                )
            )

    assert calls, "FR-101 (AC-101.b): agent_step._validate_reused_terminal must invoke the guard"
    assert calls[0] == {"engine": KiroEngine.KAS}


def test_all_seven_call_site_modules_import_the_guard() -> None:
    """A cheap completeness cross-check on AC-101.b's per-site coverage."""
    expected = {
        "services/terminal_service.py",
        "providers/manager.py",
        "providers/kiro_cli.py",
        "services/agent_step.py",
    }
    importers = {
        str(path.relative_to(_SRC_ROOT))
        for path in _SRC_ROOT.rglob("*.py")
        if "assert_kas_launch_allowed" in path.read_text(encoding="utf-8")
        and str(path.relative_to(_SRC_ROOT)) != _GUARD_MODULE
    }
    assert expected <= importers, (
        f"FR-101: these former raise-site modules do not reference the guard: "
        f"{sorted(expected - importers)}"
    )
