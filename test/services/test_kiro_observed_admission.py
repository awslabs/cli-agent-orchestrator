"""Observed admission uses fake probes and panes; no operator Kiro is touched."""

import json
import logging
import subprocess
from dataclasses import replace
from test.services.test_ephemeral_service import create_store  # noqa: F401
from test.services.test_kiro_helper_launch import (  # noqa: F401
    CHILD,
    assert_refused_before_bind,
    home,
    kiro,
    kiro_events,
    launch,
    operator_home,
)
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers import kiro_launch_home, kiro_v3_schema
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroCapabilities,
    KiroCapabilityError,
    probe_kiro_capabilities,
)
from cli_agent_orchestrator.services import kiro_v3_launch, settings_service, terminal_service


@pytest.fixture
def observed(monkeypatch):
    monkeypatch.setattr(
        settings_service,
        "_load",
        lambda: {"kiro": {"v3_admission": "observed", "v3_allow_unverified_versions": True}},
    )
    monkeypatch.setattr(kiro_v3_launch, "kiro_v3_allow_unverified_versions", lambda: True)


def probe(version, engines="v2|v3"):
    def runner(argv, **kwargs):
        output = (
            f"kiro-cli {version}"
            if argv[-1] == "--version"
            else f"--agent-engine {engines}\n--agent NAME\n--trust-all-tools\n"
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    return probe_kiro_capabilities(KiroEngine.V3, [], runner=runner, allow_observed=True)


@pytest.mark.parametrize("value", [None, True, "Observed", "observed ", 1, {}, []])
def test_only_exact_observed_setting_admits(value, monkeypatch):
    monkeypatch.setattr(settings_service, "_load", lambda: {"kiro": {"v3_admission": value}})
    monkeypatch.setenv("CAO_KIRO_V3_ADMISSION", "observed")
    with pytest.raises(KiroCapabilityError) as err:
        probe("3.2.0")
    assert err.value.kind == "kiro_v3_unsupported_version"


def test_missing_setting_is_strict(monkeypatch):
    monkeypatch.setattr(settings_service, "_load", lambda: {})
    with pytest.raises(KiroCapabilityError) as err:
        probe("3.2.0")
    assert err.value.kind == "kiro_v3_unsupported_version"


@pytest.mark.parametrize("version", ["4.0.0", "4.1.2", "2.35.0", "3.2", "3.2.0-beta", "garbage"])
def test_observed_version_refusals(version, observed):
    with pytest.raises(KiroCapabilityError) as err:
        probe(version)
    assert err.value.kind == "kiro_v3_observed_refused"
    assert "Use a supported" in str(err.value)


def test_observed_requires_v3_probe(observed):
    with pytest.raises(KiroCapabilityError) as err:
        probe("3.2.0", "v2")
    assert err.value.kind == "kiro_v3_observed_refused"
    assert "--agent-engine" in str(err.value)


@pytest.mark.parametrize("rows", ["cross-major", "future"])
def test_observed_requires_same_major_predecessor(observed, monkeypatch, rows):
    table = dict(kiro_v3_schema.SUPPORTED_KIRO_CLI_VERSIONS)
    entry = table["3.1.0"]
    table = {k: v for k, v in table.items() if k.startswith("2.")}
    if rows == "future":
        table["3.3.0"] = replace(entry, cli_version="3.3.0")
    monkeypatch.setattr(kiro_v3_schema, "SUPPORTED_KIRO_CLI_VERSIONS", table)
    with pytest.raises(KiroCapabilityError) as err:
        probe("3.2.0")
    assert err.value.kind == "kiro_v3_observed_refused"
    assert "same major" in str(err.value)


@pytest.mark.parametrize("version, borrowed", [("3.2.0", "3.1.0"), ("2.36.5", "2.36.0")])
def test_observed_pending_schema_needs_allow_unverified(monkeypatch, version, borrowed):
    monkeypatch.setattr(settings_service, "_load", lambda: {"kiro": {"v3_admission": "observed"}})
    with pytest.raises(KiroCapabilityError) as err:
        probe(version)
    assert err.value.kind == "kiro_v3_unverified_version"
    assert str(err.value) == (
        f"kiro_v3_unverified_version: kiro-cli {version} would borrow kiro-cli "
        f"{borrowed}'s schema, which is pending operator verification, so CAO does "
        "not launch v3 on it. Use a supported verified kiro-cli version, or enable "
        "kiro.v3_allow_unverified_versions to admit this borrowed schema."
    )


@pytest.mark.parametrize("observed_mode", [False, True])
def test_listed_pending_refusal_text_unchanged(observed_mode):
    gate = (
        kiro_v3_schema.observed_kiro_v3_entry
        if observed_mode
        else kiro_v3_schema.require_launchable_kiro_v3_version
    )
    with pytest.raises(kiro_v3_schema.KiroV3UnverifiedVersionError) as err:
        gate("3.1.0")
    assert str(err.value) == (
        "kiro_v3_unverified_version: kiro-cli 3.1.0's v3 engine schema was derived "
        "statically and is pending operator verification, so CAO does not launch "
        "v3 on it. Retry with engine 'v2'."
    )


def test_observed_refuses_newest_pending_even_with_older_verified_schema(monkeypatch):
    table = dict(kiro_v3_schema.SUPPORTED_KIRO_CLI_VERSIONS)
    table["3.0.0"] = replace(table["3.0.0"], pending_operator_verification=False)
    monkeypatch.setattr(kiro_v3_schema, "SUPPORTED_KIRO_CLI_VERSIONS", table)
    monkeypatch.setattr(settings_service, "_load", lambda: {"kiro": {"v3_admission": "observed"}})
    with pytest.raises(KiroCapabilityError) as err:
        probe("3.2.0")
    assert err.value.kind == "kiro_v3_unverified_version"
    assert "3.1.0" in str(err.value)
    caps = KiroCapabilities(version="3.2.0", flags=frozenset(), agent_engines=frozenset({"v3"}))
    with pytest.raises(kiro_v3_schema.KiroV3UnverifiedVersionError) as gate:
        kiro_v3_launch.require_kiro_v3_admissible(caps, ["fs_read"], allow_observed=True)
    assert gate.value.version == "3.1.0"


@pytest.mark.asyncio
async def test_observed_only_2380_refuses_newest_pending_row(kiro, monkeypatch):
    (kiro.fake / "version").write_text("2.38.0")
    monkeypatch.setattr(settings_service, "_load", lambda: {"kiro": {"v3_admission": "observed"}})
    with pytest.raises(kiro.service.EphemeralPolicyError) as err:
        await launch(kiro)
    assert err.value.rule == "kiro_v3_unverified_version"
    assert "2.37.1" in err.value.message
    assert_refused_before_bind(kiro)


def test_observed_2380_with_both_settings_borrows_newest_pending_row(observed):
    admission = kiro_v3_launch.require_kiro_v3_admissible(
        probe("2.38.0"), ["fs_read"], allow_observed=True
    )
    assert admission.borrowed_schema_version == "2.37.1"
    assert admission.unverified_version == "2.37.1"


@pytest.mark.parametrize(
    "version",
    ["03.2.0", "3.0.00", "02.35.1", "3." + "9" * 5000 + ".0", "3.1000000.0"],
    ids=["leading-major", "leading-patch", "leading-listed", "huge-part", "seven-digit-part"],
)
def test_observed_odd_version_refusals(version, observed):
    with pytest.raises(KiroCapabilityError) as err:
        probe(version)
    assert err.value.kind == "kiro_v3_observed_refused"


def test_observed_parse_value_error_is_typed_refusal(observed, monkeypatch):
    def invalid_int(part):
        raise ValueError("invalid version part")

    monkeypatch.setattr(kiro_v3_schema, "int", invalid_int, raising=False)
    with pytest.raises(KiroCapabilityError) as err:
        probe("3.2.0")
    assert err.value.kind == "kiro_v3_observed_refused"


@pytest.mark.parametrize("version", ["３.2.0", "3.٢.0", "3.2٢.0"])
def test_observed_unicode_digits_refused(version, observed):
    with pytest.raises(KiroCapabilityError) as err:
        probe(version)
    assert err.value.kind == "kiro_v3_observed_refused"


def test_observed_rows_compare_numerically(observed, monkeypatch):
    entry = kiro_v3_schema.SUPPORTED_KIRO_CLI_VERSIONS["3.1.0"]
    table = {
        v: replace(entry, cli_version=v, pending_operator_verification=False)
        for v in ("3.9.0", "3.10.0")
    }
    monkeypatch.setattr(kiro_v3_schema, "SUPPORTED_KIRO_CLI_VERSIONS", table)
    admission = kiro_v3_launch.require_kiro_v3_admissible(
        probe("3.11.0"), ["fs_read"], allow_observed=True
    )
    assert admission.borrowed_schema_version == "3.10.0"
    # A lexical upper bound would also include the future 3.10.0 row here.
    assert kiro_v3_schema.observed_kiro_v3_entry("3.9.1").cli_version == "3.9.0"


def test_observed_setting_rechecked_between_probe_and_gate(observed, monkeypatch):
    caps = probe("3.2.0")
    monkeypatch.setattr(settings_service, "_load", lambda: {"kiro": {"v3_admission": "strict"}})
    with pytest.raises(kiro_v3_schema.KiroV3UnsupportedVersionError):
        kiro_v3_launch.require_kiro_v3_admissible(caps, ["fs_read"], allow_observed=True)


@pytest.mark.asyncio
async def test_observed_pending_warning_and_notes_preserved(kiro, observed, caplog):
    (kiro.fake / "version").write_text("3.2.0")
    with caplog.at_level(logging.WARNING):
        terminal = await launch(kiro)
    notes = " ".join(terminal.launch_notes)
    assert "(pending operator verification)" in notes
    assert terminal.launch_notes[1] == (
        "kiro_v3_unverified_version_admitted: kiro-cli 3.2.0 runs on the schema of "
        "kiro-cli 3.1.0, which is pending operator verification; this v3 launch was "
        "admitted by the operator opt-in kiro.v3_allow_unverified_versions."
    )
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert (
        "Kiro v3 launch admitted: kiro-cli 3.2.0 runs on the schema of kiro-cli 3.1.0, "
        "which is pending operator verification, by the operator opt-in "
        "kiro.v3_allow_unverified_versions (#1167). Keep the opt-in off outside the "
        "operator checklist."
    ) in warnings
    assert any(
        "observed admission" in m and "(pending operator verification)" in m for m in warnings
    )


def test_listed_pending_warning_and_notes_unchanged(observed, caplog):
    caps = KiroCapabilities(version="3.1.0", flags=frozenset(), agent_engines=frozenset({"v3"}))
    with caplog.at_level(logging.WARNING):
        admission = kiro_v3_launch.require_kiro_v3_admissible(
            caps, ["fs_read"], allow_observed=True
        )
    assert admission.observed_version is None
    assert admission.borrowed_schema_version is None
    assert admission.launch_notes == [
        "kiro_v3_unverified_version_admitted: kiro-cli 3.1.0 is pending operator "
        "verification; this v3 launch was admitted only by the operator opt-in "
        "kiro.v3_allow_unverified_versions."
    ]
    assert [r.message for r in caplog.records if r.levelno == logging.WARNING] == [
        "Kiro v3 launch admitted on kiro-cli 3.1.0, which is pending operator "
        "verification, by the operator opt-in kiro.v3_allow_unverified_versions "
        "(#1167). Keep the opt-in off outside the operator checklist."
    ]


def test_observed_verified_schema_needs_no_pending_opt_in(monkeypatch, caplog):
    monkeypatch.setattr(settings_service, "_load", lambda: {"kiro": {"v3_admission": "observed"}})
    assert not settings_service.kiro_v3_allow_unverified_versions()
    with caplog.at_level(logging.WARNING):
        admission = kiro_v3_launch.require_kiro_v3_admissible(
            probe("2.35.2"), ["fs_read"], allow_observed=True
        )
    assert admission.observed_version == "2.35.2"
    assert admission.borrowed_schema_version == "2.35.1"
    assert admission.unverified_version is None
    assert admission.launch_notes == [
        "kiro_v3_observed_admitted: kiro-cli 2.35.2 borrowed schema from kiro-cli "
        "2.35.1 by operator opt-in kiro.v3_admission=observed; every live launch "
        "check still applies."
    ]
    assert [r.message for r in caplog.records if r.levelno == logging.WARNING] == [
        "Kiro v3 observed admission: kiro-cli 2.35.2 borrowed schema from kiro-cli "
        "2.35.1; operator opt-in kiro.v3_admission=observed; live checks remain required."
    ]
    assert "pending" not in " ".join(admission.launch_notes)
    assert "pending" not in caplog.text


@pytest.mark.asyncio
async def test_observed_admission_recorded_in_prepared_event(kiro, observed):
    (kiro.fake / "version").write_text("3.2.0")
    await launch(kiro)
    prepared = kiro_events(kiro)["kiro_home_prepared"]
    assert prepared["admission"] == "observed"
    assert prepared["kiro_version"] == "3.2.0"
    assert prepared["borrowed_from"] == "3.1.0"


@pytest.mark.asyncio
async def test_observed_helper_borrows_schema_and_reports_versions(kiro, observed, caplog):
    (kiro.fake / "version").write_text("3.2.0")
    with caplog.at_level(logging.WARNING):
        terminal = await launch(kiro)
    rendered = kiro.factory.call_args.kwargs["kiro_launch"]
    assert rendered.version == "3.2.0"
    assert rendered.schema == kiro_v3_schema.SUPPORTED_KIRO_CLI_VERSIONS["3.1.0"].schema.engine
    agent = json.loads((home() / "agents" / f"{kiro.name}.json").read_text())
    assert agent["permissions"]["rules"]
    notes = " ".join(terminal.launch_notes)
    assert "3.2.0" in notes and "3.1.0" in notes and "observed" in notes
    assert any(
        r.levelno == logging.WARNING and "3.2.0" in r.message and "3.1.0" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_observed_listed_version_still_requires_existing_opt_in(kiro, monkeypatch):
    (kiro.fake / "version").write_text("3.1.0")
    monkeypatch.setattr(settings_service, "_load", lambda: {"kiro": {"v3_admission": "observed"}})
    with pytest.raises(kiro.service.EphemeralPolicyError) as err:
        await launch(kiro)
    assert err.value.rule == "kiro_v3_unverified_version"
    assert_refused_before_bind(kiro)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "pass",
        "extra-tool",
        "extra-mcp",
        "missing-mcp",
        "unparsable",
        "fallback",
        "wrong-header",
        "global-marker",
        "sealed-home",
    ],
)
async def test_observed_live_refusal_tears_down_before_task(kiro, observed, monkeypatch, fault):
    from test.providers.test_kiro_cli_v3_launch import FakePane, _launch, _screen

    from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
    from cli_agent_orchestrator.providers.kiro_identity import KiroAgentIdentityError
    from cli_agent_orchestrator.providers.kiro_v3_schema import KiroV3Refusal
    from cli_agent_orchestrator.providers.manager import ProviderManager

    monkeypatch.setattr(kiro_launch_home, "stop_kiro_home_processes", lambda home: True)
    (kiro.fake / "version").write_text("3.2.0")
    options = {}
    if fault == "extra-tool":
        options["tools"] = "tools_shell_visible"
    elif fault == "extra-mcp":
        options["mcp"] = ("mcp_decoys_shown",)
    elif fault == "missing-mcp":
        options["mcp"] = (_screen("mcp_running").replace("cao-mcp-server", "other-server"),)
    elif fault == "unparsable":
        options["tools"] = "\nunknown panel\n"
    elif fault == "fallback":
        options["ready"] = "fallback_not_found"
    pane = FakePane(str(kiro.repo), **options)
    original_history = pane.get_history

    def history(*a, **kw):
        agent = "unrequested-agent" if fault == "wrong-header" else kiro.name
        return original_history(*a, **kw).replace("v3ro", agent)

    pane.get_history = history
    original_initialize = KiroCliProvider.initialize
    original_identity = KiroCliProvider._verify_agent_identity
    providers = []

    async def verify_identity(self):
        await original_identity(self)
        if fault == "global-marker":
            kiro.operator.set_entry(".secrets-migrated", "file")
        elif fault == "sealed-home":
            settings = home() / "settings" / "cli.json"
            settings.chmod(0o600)
            settings.write_text("{}")

    monkeypatch.setattr(KiroCliProvider, "_verify_agent_identity", verify_identity)

    async def initialize(self):
        providers.append(self)
        with patch.object(KiroCliProvider, "initialize", original_initialize):
            return await _launch(pane, provider=self, init_timeout=1)

    monkeypatch.setattr(KiroCliProvider, "initialize", initialize)
    manager = terminal_service.provider_manager
    kiro.factory.side_effect = lambda *a, **kw: ProviderManager.create_provider(manager, *a, **kw)
    if fault == "pass":
        terminal = await launch(kiro)
        assert providers[0].identity_verified and providers[0].attestation is not None
        assert pane.keys == [*"/mcp", "Enter", "Escape", *"/tools", "Enter", "Escape"]
        assert "3.2.0" in " ".join(terminal.launch_notes)
        manager._providers.pop(CHILD, None)
        return
    with pytest.raises((KiroV3Refusal, KiroAgentIdentityError)):
        await launch(kiro)
    assert providers and not providers[0].identity_verified
    kiro.backend.kill_window.assert_called_once_with("cao-session", "child")
    assert pane.texts == [pane.command]
    if fault in {"wrong-header", "global-marker", "sealed-home"}:
        assert pane.keys == []
    assert not home().exists()
    assert database.get_terminal_metadata(CHILD) is None
    assert database.get_ephemeral_agent(kiro.name)["gc_reason"] == "launch_failed"
    manager._providers.pop(CHILD, None)


@pytest.mark.parametrize(("version", "predecessor"), [("2.35.0", "2.34.0"), ("4.1.2", "4.0.1")])
def test_observed_bounds_apply_even_with_eligible_schema(
    observed, monkeypatch, version, predecessor
):
    table = dict(kiro_v3_schema.SUPPORTED_KIRO_CLI_VERSIONS)
    table[predecessor] = replace(table["2.35.1"], cli_version=predecessor)
    monkeypatch.setattr(kiro_v3_schema, "SUPPORTED_KIRO_CLI_VERSIONS", table)
    with pytest.raises(KiroCapabilityError) as err:
        probe(version)
    assert err.value.kind == "kiro_v3_observed_refused"


def test_launch_rechecks_observed_v3_capability(observed):
    caps = KiroCapabilities(version="3.2.0", flags=frozenset(), agent_engines=frozenset({"v2"}))
    with pytest.raises(kiro_v3_schema.KiroV3Refusal) as err:
        kiro_v3_launch.require_kiro_v3_admissible(caps, ["fs_read"], allow_observed=True)
    assert err.value.code == "kiro_v3_observed_refused"


def test_non_helper_probe_stays_strict_with_observed_setting(observed):
    def runner(argv, **kwargs):
        output = "kiro-cli 3.2.0" if argv[-1] == "--version" else "--agent-engine v2|v3\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    with pytest.raises(KiroCapabilityError) as err:
        probe_kiro_capabilities(KiroEngine.V3, [], runner=runner)
    assert err.value.kind == "kiro_v3_unsupported_version"


def test_non_helper_launch_gate_stays_strict_with_observed_setting(observed):
    caps = KiroCapabilities(version="3.2.0", flags=frozenset(), agent_engines=frozenset({"v3"}))
    with pytest.raises(kiro_v3_schema.KiroV3UnsupportedVersionError):
        kiro_v3_launch.require_kiro_v3_admissible(caps, ["fs_read"])


@pytest.mark.asyncio
async def test_installed_agent_launch_stays_strict_with_observed_setting(observed, tmp_path):
    from test.services.test_kiro_v3_launch import PROFILE, _create

    caps = KiroCapabilities(version="3.2.0", flags=frozenset(), agent_engines=frozenset({"v3"}))
    seen = {}
    with pytest.raises(kiro_v3_schema.KiroV3UnsupportedVersionError):
        await _create(PROFILE, tmp_path, seen=seen, probe=lambda *a, **kw: caps)
    assert seen == {"session": False, "other": False}


@pytest.mark.asyncio
async def test_default_helper_on_320_is_unsupported(kiro, monkeypatch):
    (kiro.fake / "version").write_text("3.2.0")
    monkeypatch.setattr(settings_service, "_load", lambda: {})
    with pytest.raises(kiro.service.EphemeralPolicyError) as err:
        await launch(kiro)
    assert err.value.rule == "kiro_v3_unsupported_version"
    assert_refused_before_bind(kiro)


@pytest.mark.asyncio
async def test_observed_alone_helper_on_320_is_unverified(kiro, monkeypatch):
    (kiro.fake / "version").write_text("3.2.0")
    monkeypatch.setattr(settings_service, "_load", lambda: {"kiro": {"v3_admission": "observed"}})
    with pytest.raises(kiro.service.EphemeralPolicyError) as err:
        await launch(kiro)
    assert err.value.rule == "kiro_v3_unverified_version"
    assert "3.1.0" in err.value.message
    assert_refused_before_bind(kiro)


@pytest.mark.parametrize("shape", ["malformed", "unreadable", "absent"])
def test_unreadable_or_absent_settings_file_is_strict(tmp_path, monkeypatch, shape):
    settings = tmp_path / "settings.json"
    if shape == "malformed":
        settings.write_text("{invalid json")
    elif shape == "unreadable":
        settings.mkdir()
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings)
    with pytest.raises(KiroCapabilityError) as err:
        probe("3.2.0")
    assert err.value.kind == "kiro_v3_unsupported_version"


def test_observed_is_enabled_from_settings_file_only(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"kiro": {"v3_admission": "observed", "v3_allow_unverified_versions": True}})
    )
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings)
    assert probe("3.2.0").version == "3.2.0"
