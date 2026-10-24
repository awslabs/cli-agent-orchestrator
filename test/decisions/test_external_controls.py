import asyncio
import json
import threading
from importlib.metadata import EntryPoint
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.decisions import decisions
from cli_agent_orchestrator.decisions import egress, operator, settings
from cli_agent_orchestrator.services import settings_service


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_service, "CAO_HOME_DIR", tmp_path)
    for key in settings.STATE_ENV.values():
        monkeypatch.delenv(key, raising=False)


def package(name="external", distribution="external-decider", script=True, code=7):
    main = Mock(return_value=code)
    command = SimpleNamespace(
        name="cao-decider-" + name, group="console_scripts", load=lambda: main
    )
    dist = SimpleNamespace(
        metadata={"Name": distribution, "Version": "1.2.3"},
        version="1.2.3",
        entry_points=[command] if script else [],
    )
    entry = EntryPoint(name=name, value="unused:Decider", group="cao.deciders")
    entry = entry._for(dist)
    return entry, main


def test_status_lists_metadata_without_importing(monkeypatch):
    from cli_agent_orchestrator.decisions import packages

    builtin, _ = package("fixed_table", "cli_agent_orchestrator", False)
    external, _ = package()
    monkeypatch.setattr(packages, "entry_points", lambda **kwargs: [external, builtin])
    monkeypatch.setattr(EntryPoint, "load", lambda self: pytest.fail("decider imported"))
    status = json.loads(CliRunner().invoke(decisions, ["status"]).output)
    assert status["halted"] is False
    assert status["installed_deciders"] == [
        {
            "name": "external",
            "distribution": "external-decider",
            "version": "1.2.3",
            "kind": "external",
            "disclosure": "sends data off this machine only after its own consent",
        },
        {
            "name": "fixed_table",
            "distribution": "cli_agent_orchestrator",
            "version": "1.2.3",
            "kind": "builtin",
        },
    ]
    egress.HALT_FILE.touch()
    assert operator.status()["halted"]


@pytest.mark.parametrize(
    "command,action", [("enable", "consent"), ("disable", "revoke"), ("show", "status")]
)
@pytest.mark.parametrize("code", [0, 7])
def test_dispatch_console_script_from_matching_distribution(monkeypatch, command, action, code):
    from cli_agent_orchestrator.decisions import packages

    entry, main = package(code=code)
    monkeypatch.setattr(packages, "entry_points", lambda **kwargs: [entry])
    result = CliRunner().invoke(decisions, [command, "external", "--custom", "value"])
    assert result.exit_code == code, result.output
    main.assert_called_once_with([action, "--custom", "value"])


@pytest.mark.parametrize(
    "case", ["missing", "no_script", "duplicate", "wrong_group", "no_distribution"]
)
def test_dispatch_refuses_missing_or_ambiguous_metadata(monkeypatch, case):
    from cli_agent_orchestrator.decisions import packages

    entry, main = package(script=case != "no_script")
    entries = [entry]
    if case == "missing":
        entries = []
    elif case == "duplicate":
        entries.append(package(distribution="other")[0])
    elif case == "wrong_group":
        entry.dist.entry_points[0].group = "cao.deciders"
    elif case == "no_distribution":
        entries = [EntryPoint(name="external", value="unused:Decider", group="cao.deciders")]
    monkeypatch.setattr(packages, "entry_points", lambda **kwargs: entries)
    result = CliRunner().invoke(decisions, ["enable", "external"])
    assert result.exit_code == 2
    main.assert_not_called()
    if case in ("no_script", "wrong_group", "no_distribution"):
        assert "no operator commands for external" in result.output


def test_builtin_has_no_operator_commands():
    result = CliRunner().invoke(decisions, ["show", "fixed_table"])
    assert result.exit_code == 2
    assert "no operator commands for fixed_table" in result.output


def test_off_overrides_environment_and_preserves_unrelated_settings(monkeypatch):
    settings.set_point("model.route", "on", "external")
    raw = settings_service._load_or_raise()
    raw["unrelated"] = {"keep": True}
    settings_service._save(raw)
    monkeypatch.setenv("CAO_DECISION_MODEL_ROUTE", "on")
    result = CliRunner().invoke(decisions, ["off"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["halted"] and data["drained"] and data["settings_saved"]
    assert all(point["state"] == "off" for point in data["points"].values())
    raw = settings_service._load_or_raise()
    assert raw["unrelated"] == {"keep": True}
    assert {point["state"] for point in raw["decisions"]["points"].values()} == {"off"}
    assert raw["decisions"]["points"]["model.route"]["decider"] == "external"


@pytest.mark.parametrize("contents", ["broken", '{"decisions": {"points": []}}'])
def test_off_works_with_unreadable_or_malformed_settings(contents):
    settings_service.SETTINGS_FILE.write_text(contents)
    result = CliRunner().invoke(decisions, ["off"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["halted"] and data["drained"]
    assert not data["settings_saved"]
    assert {point["state"] for point in data["points"].values()} == {"off"}
    assert settings_service.SETTINGS_FILE.read_text() == contents


def test_off_uses_shadow_timeout_and_cli_reports_drain_failure(monkeypatch):
    settings_service.SETTINGS_FILE.write_text('{"decisions": {"shadow": {"timeout_ms": 250}}}')
    timeouts = []

    def drain(timeout):
        timeouts.append(timeout)
        return False

    monkeypatch.setattr(egress, "drain", drain)
    result = CliRunner().invoke(decisions, ["off"])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["drained"] is False
    assert timeouts == [5.25]
    assert egress.is_halted()


@pytest.mark.asyncio
async def test_cli_off_waits_for_permit_and_refuses_later_sends():
    settings.set_point("model.route", "on", "external")
    done = threading.Event()
    output = []

    def stop():
        output.append(CliRunner().invoke(decisions, ["off"]))
        done.set()

    thread = threading.Thread(target=stop)
    try:
        async with egress.egress_permit("model.route", "external") as allowed:
            assert allowed
            thread.start()
            # The marker confirms off has begun its ordered work, not just that
            # the thread was scheduled. It must remain blocked on our permit.
            for _ in range(200):
                if egress.is_halted():
                    break
                await asyncio.sleep(0.005)
            assert egress.is_halted()
            assert not done.wait(0.03)
            async with egress.egress_permit("model.route", "external") as granted:
                assert not granted
    finally:
        if thread.ident is not None:
            thread.join(2)
    assert done.is_set() and output[0].exit_code == 0
    async with egress.egress_permit("model.route", "external") as granted:
        assert not granted


def test_ops_off_shares_control_and_cannot_grant_consent(monkeypatch):
    from cli_agent_orchestrator.ops_mcp_server.decision_tools import register_decision_tools

    tools = {}
    register_decision_tools(SimpleNamespace(tool=lambda fn: tools.setdefault(fn.__name__, fn)))
    settings.set_point("model.route", "on")
    assert tools["decisions_off"]()["success"]
    assert egress.is_halted()
    assert set(tools) == {
        "decisions_status",
        "decisions_off",
        "decisions_set_point",
        "decisions_set_tier",
        "decisions_set_exclusions",
        "decisions_list",
        "decisions_purge",
    }
    monkeypatch.setattr(egress, "drain", lambda timeout: False)
    result = tools["decisions_off"]()
    assert not result["success"] and result["data"]["halted"]


def test_enabling_point_prints_effective_states():
    egress.HALT_FILE.touch()
    result = CliRunner().invoke(decisions, ["set", "model.route", "shadow"])
    assert result.exit_code == 0
    assert json.loads(result.output)["points"]["model.route"]["state"] == "shadow"
    assert not egress.is_halted()


@pytest.mark.parametrize(
    "case", ["missing_metadata", "duplicate_script", "not_callable", "raises", "bad_exit"]
)
def test_metadata_and_script_errors_fail_closed(monkeypatch, case):
    from cli_agent_orchestrator.decisions import packages

    entry, main = package()
    if case == "missing_metadata":
        entry.dist.metadata = {}
    elif case == "duplicate_script":
        entry.dist.entry_points.append(entry.dist.entry_points[0])
    elif case == "not_callable":
        entry.dist.entry_points[0].load = lambda: None
    elif case == "raises":
        main.side_effect = RuntimeError("private package error")
    elif case == "bad_exit":
        main.return_value = None
    monkeypatch.setattr(packages, "entry_points", lambda **kwargs: [entry])
    if case == "missing_metadata":
        result = operator.status()["installed_deciders"]
        assert result[0]["kind"] == "external" and result[0]["distribution"] is None
    else:
        result = CliRunner().invoke(decisions, ["enable", "external"])
        assert result.exit_code != 0
        assert "private package error" not in result.output


def test_off_drains_even_when_settings_write_fails(monkeypatch):
    settings.set_point("model.route", "on")

    def fail(data):
        raise OSError("write failed")

    monkeypatch.setattr(settings_service, "_save", fail)
    result = operator.off()
    assert result["drained"] and result["halted"] and not result["settings_saved"]
    assert result["points"]["model.route"]["state"] == "off"


def test_off_does_not_claim_success_when_halt_creation_fails(monkeypatch, tmp_path):
    parent = tmp_path / "not-a-directory"
    parent.touch()
    monkeypatch.setattr(egress, "HALT_FILE", parent / "decisions.halt")
    result = CliRunner().invoke(decisions, ["off"])
    assert result.exit_code != 0


def test_off_never_opens_unredirected_home_files(monkeypatch, tmp_path):
    import io
    import os
    from pathlib import Path

    from cli_agent_orchestrator.constants import CAO_HOME_DIR

    protected = {CAO_HOME_DIR / "decisions.halt", CAO_HOME_DIR / "decisions-egress.lock"}
    original_os_open, original_io_open = os.open, io.open

    def guard(original):
        def open_file(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                assert Path(path) not in protected, "unredirected decision state file"
            return original(path, *args, **kwargs)

        return open_file

    monkeypatch.setattr(os, "open", guard(original_os_open))
    monkeypatch.setattr(io, "open", guard(original_io_open))
    result = operator.off()
    assert result["drained"]
    assert egress.HALT_FILE == tmp_path / "decisions.halt"
    assert egress.EGRESS_LOCK_FILE == tmp_path / "decisions-egress.lock"


@pytest.mark.parametrize("reenable", [False, True])
def test_off_reports_concurrent_reenable_on_cli_and_ops(monkeypatch, reenable):
    from cli_agent_orchestrator.ops_mcp_server.decision_tools import register_decision_tools

    def drain(timeout):
        egress.HALT_FILE.unlink()
        if reenable:
            settings.set_point("effort.route", "shadow")
        return True

    monkeypatch.setattr(egress, "drain", drain)
    message = "A concurrent change re-enabled decisions during off; re-run cao decisions off."
    result = CliRunner().invoke(decisions, ["off"])
    assert result.exit_code == 1
    assert result.stderr.strip() == message
    data = json.loads(result.stdout)
    assert data["drained"] and not data["halted"]
    assert not egress.HALT_FILE.exists()
    if reenable:
        assert data["points"]["effort.route"]["state"] == "shadow"
    tools = {}
    register_decision_tools(SimpleNamespace(tool=lambda fn: tools.setdefault(fn.__name__, fn)))
    result = tools["decisions_off"]()
    assert not result["success"]
    assert result["message"] == message
    assert result["data"]["drained"] and not result["data"]["halted"]
    assert not egress.HALT_FILE.exists()


def test_off_checks_returned_point_states_even_if_halted(monkeypatch):
    from dataclasses import replace

    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    actual = operator.load_settings

    def load():
        snapshot = actual()
        return replace(
            snapshot, points={**snapshot.points, "effort.route": PointSettings(PointState.SHADOW)}
        )

    monkeypatch.setattr(operator, "load_settings", load)
    result = CliRunner().invoke(decisions, ["off"])
    assert result.exit_code == 1
    assert "concurrent change re-enabled decisions" in result.stderr
    assert json.loads(result.stdout)["halted"]


def test_off_publishes_halt_before_saving(monkeypatch):
    actual_save = settings_service._save

    def save(data):
        assert egress.HALT_FILE.exists(), "settings saved before halt was published"
        actual_save(data)

    monkeypatch.setattr(settings_service, "_save", save)
    result = operator.off()
    assert result["drained"] and result["halted"] and result["settings_saved"]


@pytest.mark.parametrize("code,expected", [(256, 1), (-1, 1), (255, 255)])
def test_dispatch_normalizes_out_of_range_exit_codes(monkeypatch, code, expected):
    from cli_agent_orchestrator.decisions import packages

    entry, main = package(code=code)
    monkeypatch.setattr(packages, "entry_points", lambda **kwargs: [entry])
    assert packages.dispatch("external", "status") == expected
    assert CliRunner().invoke(decisions, ["show", "external"]).exit_code == expected


def test_status_tolerates_missing_distribution_version(monkeypatch):
    from cli_agent_orchestrator.decisions import packages

    class Distribution:
        metadata = {"Name": "external-decider"}

        @property
        def version(self):
            pytest.fail("missing Version accessed through distribution.version")

    entry = EntryPoint(name="external", value="unused:Decider", group="cao.deciders")._for(
        Distribution()
    )
    monkeypatch.setattr(packages, "entry_points", lambda **kwargs: [entry])
    assert packages.installed_deciders()[0]["version"] is None


def test_off_rejects_replaced_halt_on_cli_and_ops(monkeypatch, tmp_path):
    from cli_agent_orchestrator.ops_mcp_server.decision_tools import register_decision_tools

    replacement_identity = []

    def drain(timeout):
        replacement = tmp_path / "new-halt"
        replacement.touch()
        stat = replacement.lstat()
        replacement_identity.append((stat.st_dev, stat.st_ino))
        replacement.replace(egress.HALT_FILE)
        return True

    monkeypatch.setattr(egress, "drain", drain)
    result = CliRunner().invoke(decisions, ["off"])
    assert result.exit_code == 1
    assert result.stderr.strip() == operator.OFF_REENABLED_MESSAGE
    data = json.loads(result.stdout)
    assert data["halted"] and data["drained"] and not data["success"]
    tools = {}
    register_decision_tools(SimpleNamespace(tool=lambda fn: tools.setdefault(fn.__name__, fn)))
    result = tools["decisions_off"]()
    assert not result["success"] and result["message"] == operator.OFF_REENABLED_MESSAGE
    assert result["data"]["halted"] and result["data"]["drained"]
    stat = egress.HALT_FILE.lstat()
    assert (stat.st_dev, stat.st_ino) == replacement_identity[-1]


def test_off_accepts_unchanged_preexisting_halt():
    egress.HALT_FILE.touch()
    before = egress.HALT_FILE.lstat()
    result = operator.off()
    after = egress.HALT_FILE.lstat()
    assert result["success"]
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
