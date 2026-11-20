"""The systemd units: structure, the herdr environment allowlist, loopback-only CAO.

`systemd-analyze verify` runs when it exists (Linux). On macOS it doesn't, so
a small parser checks the structure instead; the policy checks run everywhere.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from remote_ec2_support import SAMPLE_DIR

UNIT_DIR = SAMPLE_DIR / "systemd"
CORE_UNITS = ["herdr-cao.service", "cao-server.service", "cao-backup.service", "cao-backup.timer"]
ALL_UNITS = CORE_UNITS + ["cao-telegram-relay.service.example"]
HERDR_ALLOWED_ENV = {"HOME", "USER", "PATH", "LANG", "TERM"}
SECTION = re.compile(r"^\[([A-Za-z]+)\]$")


def parse_unit(path: Path) -> list[tuple[str, str, str]]:
    """Return (section, key, value) for every setting; reject anything malformed."""
    entries: list[tuple[str, str, str]] = []
    section = ""
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        match = SECTION.match(line)
        if match:
            section = match.group(1)
            continue
        assert section, f"{path.name}:{number}: setting before any [Section]"
        assert "=" in line, f"{path.name}:{number}: not key=value: {line!r}"
        key, value = line.split("=", 1)
        assert re.fullmatch(r"[A-Za-z]+", key), f"{path.name}:{number}: bad key {key!r}"
        entries.append((section, key, value))
    return entries


def values(entries, section: str, key: str) -> list[str]:
    return [v for s, k, v in entries if s == section and k == key]


def env_names(entries) -> set[str]:
    return {v.split("=", 1)[0] for v in values(entries, "Service", "Environment")}


@pytest.mark.parametrize("unit", ALL_UNITS)
def test_unit_structure(unit: str) -> None:
    entries = parse_unit(UNIT_DIR / unit)
    sections = {s for s, _, _ in entries}
    assert "Unit" in sections
    assert values(entries, "Unit", "Description")
    if unit.endswith(".timer"):
        assert values(entries, "Timer", "OnCalendar")
        assert values(entries, "Install", "WantedBy") == ["timers.target"]
        return
    assert "Service" in sections
    [exec_start] = values(entries, "Service", "ExecStart")
    if unit.endswith(".example"):
        assert exec_start == "RELAY_COMMAND"
    else:
        assert exec_start.startswith(("/usr/local/lib/cao-box/", "@BOX_HOME@/")), exec_start
    expected_user = "cao-relay" if unit.startswith("cao-telegram-relay") else "@BOX_USER@"
    assert values(entries, "Service", "User") == [expected_user]


@pytest.mark.parametrize(
    ("unit", "restart"),
    [
        ("herdr-cao.service", "always"),
        ("cao-server.service", "on-failure"),
        ("cao-telegram-relay.service.example", "on-failure"),
    ],
)
def test_long_running_units_restart_and_install(unit: str, restart: str) -> None:
    entries = parse_unit(UNIT_DIR / unit)
    assert values(entries, "Service", "Restart") == [restart]
    assert values(entries, "Install", "WantedBy") == ["multi-user.target"]


def test_herdr_unit_environment_is_the_allowlist_and_nothing_else() -> None:
    entries = parse_unit(UNIT_DIR / "herdr-cao.service")
    assert env_names(entries) == HERDR_ALLOWED_ENV
    assert values(entries, "Service", "EnvironmentFile") == []
    assert values(entries, "Service", "PassEnvironment") == []
    assert values(entries, "Service", "ExecStart") == ["/usr/local/lib/cao-box/herdr-start.sh"]


def test_herdr_is_never_part_of_cao_server() -> None:
    entries = parse_unit(UNIT_DIR / "herdr-cao.service")
    for key in ("PartOf", "BindsTo", "Requires"):
        assert values(entries, "Unit", key) == [], key


def test_cao_server_binds_loopback_only() -> None:
    entries = parse_unit(UNIT_DIR / "cao-server.service")
    [exec_start] = values(entries, "Service", "ExecStart")
    argv = exec_start.split()
    assert argv[0] == "@BOX_HOME@/.local/bin/cao-server"
    assert argv.count("--host") == 1
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    for _, _, value in entries:
        assert "0.0.0.0" not in value
    assert "CAO_ALLOWED_HOSTS" not in env_names(entries)


def test_cao_server_requires_herdr_and_waits_for_its_socket() -> None:
    entries = parse_unit(UNIT_DIR / "cao-server.service")
    assert values(entries, "Unit", "Requires") == ["herdr-cao.service"]
    assert "herdr-cao.service" in values(entries, "Unit", "After")[0].split()
    # The gate runs before cao-server: without herdr's socket, CAO would start
    # a herdr of its own (no clean layout, no minimal env, in CAO's cgroup).
    assert values(entries, "Service", "ExecStartPre") == [
        "/usr/local/lib/cao-box/wait-herdr-socket.sh"
    ]
    for key in ("PartOf", "BindsTo"):
        assert values(entries, "Unit", key) == [], key


def test_the_relay_runs_as_its_own_user_with_a_root_side_token_file() -> None:
    entries = parse_unit(UNIT_DIR / "cao-telegram-relay.service.example")
    assert values(entries, "Service", "User") == ["cao-relay"]
    assert values(entries, "Service", "Group") == ["cao-relay"]
    assert "/etc/cao-box/telegram.env" in values(entries, "Service", "EnvironmentFile")
    assert not any("@BOX_HOME@/.config" in v for v in values(entries, "Service", "EnvironmentFile"))


def test_no_core_unit_reads_a_secret_file() -> None:
    for unit in CORE_UNITS:
        entries = parse_unit(UNIT_DIR / unit)
        for path in values(entries, "Service", "EnvironmentFile"):
            assert path.lstrip("-") in {"/etc/cao-box/box.env", "/etc/cao-box/backup.env"}, (
                unit,
                path,
            )
        assert env_names(entries) <= HERDR_ALLOWED_ENV | {"AWS_REGION"}, unit


def test_only_the_relay_reads_the_telegram_token_file() -> None:
    for unit in ALL_UNITS:
        text = (UNIT_DIR / unit).read_text(encoding="utf-8")
        settings = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        if unit.startswith("cao-telegram-relay"):
            assert "telegram.env" in settings
        else:
            assert "telegram.env" not in settings, unit


def test_backup_timer_is_persistent() -> None:
    entries = parse_unit(UNIT_DIR / "cao-backup.timer")
    assert values(entries, "Timer", "Persistent") == ["true"]


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="systemd-analyze is not available"
)
def test_systemd_analyze_verify(tmp_path: Path) -> None:
    rendered = []
    for unit in CORE_UNITS:
        text = (UNIT_DIR / unit).read_text(encoding="utf-8")
        text = text.replace("@BOX_USER@", "root").replace("@BOX_HOME@", "/root")
        out = tmp_path / unit
        out.write_text(text, encoding="utf-8")
        rendered.append(str(out))
    result = subprocess.run(
        ["systemd-analyze", "verify", "--man=no", *rendered],
        capture_output=True,
        text=True,
        check=False,
    )
    # Missing executables are expected off the box; syntax problems are not.
    bad = [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if re.search(r"Unknown (key|section)|Failed to parse|Invalid", line)
    ]
    assert bad == []
